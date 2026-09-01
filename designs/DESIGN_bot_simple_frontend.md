# Design: Simple Bot Front-End ("companion mode")

**Status:** Implemented (all three rollout phases, 2026-08-25) · **Date:** 2026-08-25 ·
**Branch:** worktree-config-env-and-frontend-design

## 1. Motivation

TrainMate gets its second athlete, and she is not going to learn a command language.
The Telegram bot today is deliberately a terminal in a chat window: every message must
be a valid CLI command line, replies come back as monospace `<pre>` blocks, and the bot
never speaks first. That is exactly right for an expert operator and exactly wrong for
someone who just wants a coach.

What a receiving-first athlete needs day to day is tiny: see today's session, see the
week, tell the coach something ("I'm tired", "no time Thursday"), and glance at
progress. Three assets already in the code cover most of the machinery:

- The structured-prompt protocol (`trainmate/prompt.py`, `TRAINMATE_FRONTEND=json`)
  already renders decisions as tappable inline buttons.
- `workout adapt -m "…"` is already a free-text inbox: one LLM call classifies the
  message and extracts constraint-shaped directives (`coach/service/adaptation.py`).
- Per-athlete instances already work: `TRAINMATE_CONFIG` selects a config file, and the
  instance's `database:` lives beside it (ARCHITECTURE.md §9). One checkout, two bots,
  two tokens, zero routing code.

This design adds the missing layer: a per-instance **simple mode** where the bot opens
the day, buttons replace syntax, free text goes through an intent router, and replies
read like a coach instead of a terminal.

## 2. Goals / Non-Goals

**Goals**
- An athlete in simple mode never needs command syntax: a persistent reply keyboard
  covers the daily surface, and any free text does something sensible.
- The bot opens the day: a morning message with today's session and three buttons.
- Simple-mode replies are short prose with an encouraging frame, not `<pre>` dumps.
- Expert mode is byte-for-byte unchanged, and the mode is chosen per instance
  (`telegram.ui:` in that instance's config).
- The router runs on a configurable, cheaper model (the `router-model` setting), independent of
  the coaching model the `coach-model` setting manages.
- Every simple-mode action still executes the real CLI as a subprocess. The simple
  layer maps chat onto argv; it never re-implements domain logic. That is the same
  parity principle the bot was built on, extended rather than abandoned.

**Non-goals**
- Post-workout RPE capture in chat. Garmin owns RPE (`summaryDTO.directWorkoutRpe`,
  pulled by `garmin/client.py`; the activities upsert overwrites `rpe` on re-pull), so a
  chat-entered value would be clobbered by the next sync. She records effort in
  Garmin's own post-activity screen if she wants sRPE; with a watch, hrTSS carries load
  anyway.
- A Telegram Mini App over the web dashboard (needs a public HTTPS URL; separate
  effort), and voice-note transcription. Both compose cleanly with this design later.
- Multi-athlete routing inside one bot process. Instances solved it.
- Simple rendering for every command. Like the web dashboard, simple mode renders the
  commands worth rendering; anything else falls back to the expert `<pre>` form.

## 3. Mental model

One bot binary, two personae, chosen by the instance's config:

- `telegram.ui: expert` (default) — today's behavior, untouched.
- `telegram.ui: simple` — the companion: reply keyboard (§5.1), free-text router
  (§5.3), morning push (§4), simple rendering (§6).

Underneath both personae sits the same pipeline: message → argv → CLI subprocess →
streamed output + prompts. Simple mode changes how argv is *obtained* (buttons, router)
and how output is *shown* (rendering), never what runs.

## 4. The morning push

### 4.1 What she sees

> 🏃 **Today: easy run, 40 min** — conversational pace, HR under 145.
> Fresh legs — recovery looks good.
>
> `[ 👍 Got it ]  [ 😴 Feeling tired ]  [ 🕐 Can't today ]`

Rest days get one line ("Rest day — enjoy it 🎉", no buttons). So does a day whose
sessions are all already trained — "✅ Already done for today — nice work 💪", the
acknowledgement §6 gives the day view, promoted to the whole message. The catch-up
window runs to mid-afternoon (§4.3), so the push routinely fires on a session that is
already in the bag; briefing it back with a "can't today" row attached is a ping the
athlete cannot act on, and the schedule is the one thing she has already seen. Every
button below offers a way to change a session still ahead, so an all-done day earns
none of them, and a day where one of two sessions is done briefs the one left and keeps
them. Tapping:

- **Got it** — acknowledges, nothing runs.
- **Feeling tired** — runs `workout adapt -m "feeling tired this morning"`; the existing
  adaptation flow (including its confirm prompts) takes over.
- **Can't today** — a bot-level choose row (move it / shorten it / skip it), each option
  mapping to an `adapt -m` message. The coach decides; the buttons only phrase the ask.

### 4.2 Where the content comes from

A new hidden CLI family (`tm bot ...`, hidden like other maintenance commands):

- `tm bot morning` — renders the morning message for today (reusing the `workout list
  -d today` data path and the §6 renderer) and emits the button row via a new sentinel
  (§4.4). **Idempotent:** it records `push_morning_last = YYYY-MM-DD` in the `settings`
  table and exits silently when already sent today. All push state therefore lives in
  the instance's database; the bot process stays stateless across restarts, which is
  what lets `/restart` and crashes stay boring (DESIGN_bot_restart.md).

It takes the same `workout list` route to "what became of this session": freshen today's
activity cache, then `adherence_verdicts` (ARCHITECTURE.md §5) — one grader, so "already
trained" cannot mean one thing in the day view and another in the push. `done` and
`partial` are the statuses that count, named once in `cli/common.py` because both
surfaces read them. A grading failure degrades exactly like the adaptation below: an
aside on the terminal, the schedule briefed as stored, never a sunk push.

When `adapt-first` is on (default **off**), `tm bot morning` first runs
the daily adaptation non-interactively (`workout adapt -y`, so no prompt can strand a
scheduled run) and then renders the result — the push reflects overnight signals, and
an applied change surfaces as its one reason line ("Eased today — rough night."). Off,
the schedule renders as-is and "Feeling tired" stays the trigger for adaptation.

### 4.3 Scheduling

An asyncio task inside the bot (`python-telegram-bot` is installed without the
job-queue extra, and a sleep-until-next-fire loop needs no dependency): compute the next
`morning-time` (default `08:00`), sleep, spawn `tm bot morning` through
the ordinary `_start_command` path, repeat. Missed fires (machine asleep, bot down) are
caught up on startup/wake by the same rule: run it if the time is past but before the
deadline (default `15:00`), otherwise skip the day — a workout briefing at 9 PM is noise.

All four knobs — `push`, `morning-time`, `morning-deadline`, `adapt-first` (§4.2) — are
settings (DESIGN_settings.md): `config.yaml` seeds them under `telegram.push:`, the athlete
changes them with `settings set morning-time 07:00` from the CLI or from chat, and the loop
re-reads all four every tick, so a change lands within five minutes without a restart.
`push off` turns the push off regardless of ui mode.

The push must not collide with an in-flight command's polling pause: it uses the same
one-session-per-chat gate as typed commands (`sessions` dict) and simply retries a few
minutes later if the chat is busy.

### 4.4 Bot-level buttons: a third sentinel

The CLI↔bot channel already carries `\x1eTM-PROMPT` (blocking question) and
`\x1eTM-PHOTO` (chart hand-off), and the bot drops unknown `\x1e` sentinels rather than
leaking them — the forward-compatible slot this design uses. A new `\x1eTM-BUTTONS
{json}` line attaches a **non-blocking** inline-button row to the message just flushed:
the CLI exits without waiting, and each button carries a canned follow-up utterance the
bot feeds back through the normal pipeline when tapped (callback namespace `ui:`,
distinct from prompt nonces, valid until replaced by the next push).

Prompts ask and block; buttons offer and exit. Keeping WHAT to offer in the CLI keeps
the parity principle: the bot renders, it does not decide.

## 5. Simple-mode interaction

### 5.1 Persistent reply keyboard

Simple mode replaces the command menu with a `ReplyKeyboardMarkup` (persistent, resized)
of six buttons (two per row, in table order), each mapping to fixed argv:

| Button           | Runs                          |
|------------------|-------------------------------|
| 📅 Today         | `workout list -d today`       |
| 🗓 My week       | `workout list`                |
| 🎯 Goals         | `goal list` (§11)             |
| 🧭 My plan       | `plan show` (§11)             |
| 📈 Progress      | `progress --chart`            |
| 💬 Talk to me    | shows the capture prompt (§5.2)|

The `/start` welcome and `set_my_commands` menu get simple-mode variants to match.
Labels live in one table in `trainmate_bot.py` beside `MENU_COMMANDS`, import-safe and
unit-testable like the existing pure helpers.

### 5.2 "Talk to me"

The button is an *invitation*, not a channel. It exists so the keyboard says out
loud that free text works at all — something five view buttons otherwise hide from
a receiving-first athlete. Tapping it replies "I'm listening — what should I know?"
and changes nothing else: the next message goes through the same router (§5.3) as
any typed message and lands exactly where that message would have landed anyway.

One thing does differ, and deliberately never shows. While a tap is live, a message
the router returns `unclear` for rides the `adapt -m` inbox instead of bouncing off
`ROUTER_FALLBACK`. She has just been asked what the coach should know, so an
unreadable answer is far likelier to be a note the router failed than a stray
remark. The tap can therefore only ever *rescue* a message, never redirect one —
which is what keeps it explicable: "tap 💬 or just type, same thing" stays true in
every case the athlete can observe. The tap clears after one message, `/cancel`, or
the prompt timeout, so a next-morning message is routed normally rather than quietly
taken as a note.

**Why this is not a mode.** Until 2026-09-01 the tap *bypassed* the router: the next
message went verbatim to `adapt -m`. A question typed after a tap ("What are my
constraints?") therefore became a training note, and a full adaptation ran on it.
The rule was correct, documented, and still unusable — because nobody could state it
in one sentence to the athlete it was written for. In simple mode that is the test:
an affordance whose behaviour cannot be explained in one sentence is a design defect,
not a documentation gap. Collapse the behaviours instead of writing better copy.

### 5.3 The free-text router

Unarmed free text (anything that isn't a button label) goes to a small intent router
instead of today's "Couldn't parse that". A new hidden command `tm bot route "<text>"`
calls the router model with a fixed intent table and returns structured JSON; the bot
maps the intent back to argv **from its own table** and runs it. The model picks an
intent and slots; it never authors argv, so a hostile or confused message cannot reach
flags the table doesn't expose.

Intent table: `show_today`, `show_week`, `show_goals` / `show_plan` (§11),
`show_progress`, `coach_message` (→ `adapt -m`, carrying the original text),
`add_constraint` / `add_signal` (→ the same `adapt -m` inbox, §5.5),
`show_constraints` / `remove_constraint` (→ `bot constraints`, §5.5), `help`, `unclear`. `unclear` renders a gentle fallback with the keyboard as the suggestion. The
routed command is echoed in one short italic line ("→ showing your week") so she learns
the vocabulary and misroutes are visible immediately.

Routing through a CLI subcommand rather than in-process keeps every OpenRouter call —
client, retries, exchange logs under `logs/llm_exchanges/` — on the one existing path,
and keeps the bot importable without LLM plumbing.

### 5.4 The router model

A *role*, read by `tm bot route` only: the `router-model` setting, seeded by
`llm.router_model` in config.yaml (DESIGN_settings.md). Unset → the active coaching model,
so an install that never configured one gets no surprise second model.

It picks from `llm.models`, the same menu the coaching model picks from — one allowlist for
both roles (DESIGN_settings.md §4.1). `coach-model` and `settings set coach-model` keep
meaning the coaching model; `settings list coach-model` marks which menu entry currently
holds which role.

### 5.5 Constraints and signals in chat

"Show my rules" / "I can run again" route to a hidden `tm bot constraints`: the
current-and-upcoming directives in companion prose (day words, no IDs or tier tags)
plus a §4.4 button picker whose leaves each send the deterministic `constraint rm
<id>`. The model only ever picks the *intent*; which row is removed is decided by the
athlete's tap on a button the CLI built from real IDs.

Adding needs no new machinery, for either kind. `add_constraint` and `add_signal` land
in the same `workout adapt -m` inbox as `coach_message`, and the one call there extracts
both: the two-confirmation capture flow asks before persisting a rule
(DESIGN_constraints.md §8) or a signal (DESIGN_signal_extraction.md §2). The three
intents build identical argv and differ only in the echo line, so a misroute among them
changes what the athlete is told the coach heard, never what is stored. A kind of note
therefore earns an intent only to be echoed in its own words — never to reach a
different command.

**Why there is no `remove_signal`, and no signal counterpart to `bot constraints`.**
The two records point in opposite directions. A constraint points *forward*: it shapes
every plan and adaptation until it expires, so a stale one keeps bending the schedule,
and the athlete is the only one who knows it should go. That is what earns
`constraint rm` its place as the single tap-reachable mutation in §7. A signal points
*backward*: it records what acted on the body on days already lived, read beside the
HRV/RHR/sleep numbers as evidence, and never consulted when a future session is written.
A wrong one costs a little accuracy in one correlation; it cannot mis-shape training.
So `signal rm` stays expert-only — the cleanup is real, but it is not urgent, not
phone-shaped, and not worth widening the §7 guardrail for.

### 5.6 The `/ui` runtime switch

`/ui` flips the persona of a running bot: bare `/ui` toggles, `/ui simple` / `/ui
expert` (aliases `on`/`off`) set it explicitly, anything else prints usage. The flip is
**in-memory only** — `telegram.ui` in config.yaml is authoritative again at the next
restart — which makes it a friction-free test switch for the operator while keeping the
config the single source of truth for the wife-instance.

Mechanically, the persona flag becomes mutable process state and everything derived
from it is computed at use time: rendering env (`TRAINMATE_RENDER`), wrap width, router
vs argv parsing, plain-vs-`<pre>` replies, and the morning-push gate (the scheduler
task always runs when config enables pushes; the persona is checked per tick). The
switch itself swaps the `set_my_commands` menu and confirms with a message that
attaches the reply keyboard on the way into simple and sends `ReplyKeyboardRemove` on
the way out — Telegram clients keep the old keyboard until told otherwise.

Only the expert menu advertises `/ui`; the simple menu stays the athlete's two entries,
and the confirmation lines teach the way back.

## 6. Simple rendering

Simple mode sets `TRAINMATE_RENDER=simple` in the subprocess env (beside
`TRAINMATE_FRONTEND=json`, and interpreted in one place like `is_json_frontend`).
Commands opt in one at a time through a renderer helper in `cli/common.py`; a command
that hasn't opted in falls back to the expert `<pre>` form — the web dashboard's lesson
applied: a fallback that cannot decay beats a parity promise nobody re-checks.

Initial opt-in set: `workout list` (today/week), `progress` (chart caption + two-line
summary), the adapt result, and `bot morning`. The §11 breadth pass added `goal list`
and `plan show`.

**The adapt preview is prose too.** The first opt-in covered only the adapt *result*
(the reason line and the no-change line); the preview between them still printed the
expert table and the `-`/`+` wording diff, which simple mode then sent as flowed text —
a 160-column table and signed diff lines re-wrapped by the phone (observed 2026-08-31).
The preview now renders one paragraph per touched day under "Here's what I'd change:" —
the session line in the day-view form, a parenthetical saying what it replaces ("was
Strength — Deload Volume, 55 min", "was a rest day", "same session, wording updated"),
the coach's per-session reason when it adds to the batch reason, and for a wording-only
revision the changed passages as `Was:` / `Now:` pairs (DESIGN_workout_revisions.md
§9.1). The confirm asks "Shall I make these changes?"; the outcome lines are companion
words as well. Expert mode keeps the table, whose narrow-client form already stacks
into records.

**Tone rule** (the "not depressing" requirement): simple rendering leads with what was
done and what is next, states gaps as neutral facts after the lead, and never opens
with a miss. A skipped week reads "back on track Thursday — the coach adjusted for the
days off", not "adherence 40%". The numbers stay available; expert mode and the
dashboard are the audit surface, chat is the encouragement surface.

**A day already trained is acknowledged, a day not yet trained is not remarked on.**
The day view takes the adherence verdicts the expert listing carries
(ARCHITECTURE.md §5) and adds one line — "already done, nice work" — for a session
graded `done` or `partial`; every other verdict renders exactly as it did before.
Asking what today holds after having trained it and being read the prescription back
is the companion failing to notice, but `missed` and `pending` earn no line at all:
the tone rule keeps a gap out of the lead, and "you have not done it yet" is not news
to someone reading their own day. A `partial` counts as done here — the mismatch that
earns it is expert detail, and the chat surface does not audit.

## 7. Guardrails

- The router's intent table only reaches read-only views, `adapt -m`, and the §5.5
  constraints view. Nothing plan-shaping or expensive is routable: `plan generate`,
  `workout generate`, rollback/wipe, `model set`, `restart` require the typed expert
  vocabulary. One narrow amendment (2026-08-31, DESIGN_runway_nudge.md §6): the morning
  push's runway button may send `workout generate` (or `workout generate -m ..<id>`) —
  a fixed argv from the CLI, never reachable through the router, and still gated on the
  preview/confirm pipeline. The one destructive action a tap can reach is `constraint rm <id>`, and
  only through the §5.5 picker: single ID, offered by the CLI, chosen by the athlete —
  never by the model. (Typed commands still work in simple mode, so the operator can
  drive an instance from its own chat when allowlisted there.)
- Free text reaching the router or `adapt -m` is data, not instructions, and the argv
  table bounds its blast radius; the exposure is the same one `adapt -m` already has
  today.
- Dangerous confirmations keep the existing `danger` prompt styling and are never
  auto-answered by the simple layer.
- The push scheduler owns no state: a double-send is prevented by the `settings` marker
  (§4.2), not by the bot remembering it fired.

## 8. Touch points

| File | Change |
|---|---|
| `trainmate_bot.py` | ui-mode switch (config at start, `/ui` flips it live, §5.6), reply keyboard + label→argv table, capture-tap chat state (§5.2), `ui:` callback namespace, `TM-BUTTONS` parsing, push scheduler task |
| `trainmate/prompt.py` | `BUTTONS_SENTINEL` + `emit_buttons()` (mirror of `emit_photo`) |
| `trainmate/cli/bot.py` | new hidden family: `bot morning`, `bot route`, `bot constraints` |
| `trainmate/config.py` | `telegram_ui`; the push knobs and the router role resolve through `trainmate/settings.py` |
| `trainmate/cli/common.py` | simple renderer helper + `TRAINMATE_RENDER` interpretation |
| `docs/ARCHITECTURE.md` | §2 entry points, §9 config keys, bot section |
| `tests/` | pure-helper tests (keyboard table, sentinel codec, router table→argv, tone renderer), `bot morning` idempotency against a temp DB, `bot route` with a mocked OpenRouter |

## 9. Rollout

1. **Keyboard + rendering** (no LLM, no scheduler): she can already live in it.
2. **Morning push**: the bot starts opening the day.
3. **Router**: free text stops dead-ending.

Each phase ships alone; her onboarding starts at phase 1.

## 10. Decisions & Open Questions

**Resolved**
- RPE stays Garmin's (§2 Non-goals) — decided 2026-08-25.
- Router model is a config role, not a menu entry (§5.4).
- Second athlete = second instance via `TRAINMATE_CONFIG`; no in-bot multi-athlete.
- Bot-initiated messages are in scope (morning push first).
- Push timing: send at 08:00, catch up until a 15:00 deadline (2026-08-25). A day with
  no session gets the one-line rest message whatever the reason it is empty — no
  `weekly_schedule` special-casing.
- Strings are English; no locale table (2026-08-25).
- `adapt-first` runs the daily adaptation (non-interactive, `-y`) before
  rendering the push; default off (2026-08-25, §4.2).
- `📈 Progress` keeps its keyboard slot — to be judged in practice (2026-08-25).
- The §4.1 recovery sentence ("Fresh legs…") is NOT synthesized by the renderer: a
  heuristic recovery judgment would be coach logic living in a formatter. It appears
  when `adapt_first` ran and changed something — as the applied change's reason line —
  otherwise the push is the schedule alone (implementation, 2026-08-25).
- In simple mode a leading `/` is the expert path; bare non-slash text is the companion
  surface (labels → capture → router). Bare `help` gets the companion card; `/help
  <cmd>` still reaches the CLI tree (implementation, 2026-08-25).
- Constraints are routable (2026-08-25, §5.5): `add_constraint` rides the `adapt -m`
  capture flow; `show_constraints`/`remove_constraint` map to `bot constraints`, whose
  rm picker is the only destructive action buttons can reach — single-ID, tap-chosen.
- `/ui` flips the persona at runtime, in-memory only (2026-08-25, §5.6): a test switch
  for the operator; config.yaml stays authoritative across restarts.
- A day already trained is congratulated, not briefed, and loses the button row with it
  (2026-08-26, §4.1). The adaptation still runs first when `adapt-first` is on: it
  revises the whole forward range to the block's end, not just today, so a session
  finished before breakfast is no reason to skip the day's pass.
- Goals and the plan join the companion surface (2026-08-30, §11): two new keyboard
  buttons and router intents, `goal list` / `plan show` opted into simple rendering,
  and ✅ verdicts folded into the week view. Both new targets are read-only, so the §7
  guardrail posture is unchanged.

**Open**
1. Router echo: always show "→ …", or only when confidence is low? Draft: always;
   applies unless objected to before phase 3 (rollout §9). Implemented as: always
   (`ROUTER_ECHO` in trainmate_bot.py); trivially revisitable.

## 11. Breadth: goals, the plan, and a calendar-shaped week (2026-08-30)

The first weeks of companion mode covered the daily loop — today, the week, progress,
telling the coach. What it lacked was the *why*: an athlete without the Google Calendar
integration had no way to see what she is training toward or how the months ahead are
laid out. This pass widens the same surface without touching its rules: two more
keyboard buttons (§5.1), two more router intents (§5.3), two more commands opted into
simple rendering (§6), and no new machinery.

**Goals — `show_goals` → `goal list`.** Each goal still ahead renders as one line —
sport emoji(s), title, day word, and a countdown — followed by its description. The
countdown vocabulary (`simple_when`) is deliberately rough: days inside two weeks, then
weeks, then months — a feeling, not a schedule; it also carries the year information the
year-less day words (`simple_date_word`, 'Sat Sep 26') omit. An event goal reads "on
Sat Sep 26"; a horizon goal "by ~Wed Sep 30", the same on/by-~ wording rule as the
expert view. Completed goals compress into one celebration line; archived goals were
called off and say nothing (§6 tone rule); IDs and state tags stay expert detail. An
empty list is an invitation, not a gap.

**The plan — `show_plan` → `plan show`.** "Plan" here is the periodization —
macrocycle + mesocycles, never the scheduled sessions (those are 🗓 My week). The
companion form is the road to the goal, one line per block: done blocks get a ✅ and
nothing more, the block she is in is located by week ("you're here, week 2 of 3") and
carries the first sentence of its focus (`simple_focus_snippet` — the full prescription
is dense coach prose and stays expert detail), and future blocks get their start day and
length. The three line markers are stops on that road, sized to match the 🧭 opener and
🏁 close: ✅ behind her, 📍 where she stands, ⚪ still ahead. Badge-style emoji (🔜, and
its family) are out — at chat size they render as a coloured box with unreadable text,
and they say nothing the "starts Mon Sep 07" already says.
The goal day closes the road, reusing the goal view's wording rule. Strategy
prose, feedback, snapshotted inputs, IDs and the progress bars all stay in the expert
view; wherever that view would suggest `plan generate`, simple mode says the plan "will
appear once your goal is set up" — the athlete in companion mode cannot run it, the
operator can.

**The week doubles as her calendar.** `workout list` already computed adherence
verdicts for the listed span; the simple week view now folds them in — a trained
session's line gets a ✅, and the closing count becomes "1 of 8 sessions already done"
once anything is. `missed`/`pending` still say nothing at all, the same §6 rule the day
view follows.

Both new argv targets are read-only, so the §7 guardrail table gains two entries and
nothing else changes: plan-shaping and destructive commands still require the typed
expert vocabulary.
