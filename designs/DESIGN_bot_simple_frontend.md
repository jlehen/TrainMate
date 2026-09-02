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
distinct from prompt nonces, valid until the next row replaces it — one live row per
chat, a lifetime §12.3 has to own now that rows multiply).

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
the vocabulary and misroutes are visible immediately. The writes pass (2026-09-01)
widens this table with capture and picker intents; §12.8 is now the authoritative
table.

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
different command. *(Superseded in part, 2026-09-01: the note intents now share
the `bot capture note` inbox instead of `adapt -m` — §12.3 carries the reasoning;
the echo-only rule between `add_constraint` and `add_signal` stands.)*

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

**A tap on a keyboard the process has forgotten flips the persona back.** Telegram keeps
a persistent reply keyboard on the client until a `ReplyKeyboardRemove` tells it
otherwise, so a bot restarted into `telegram.ui: expert` leaves six live companion
buttons on a phone whose bot no longer understands them: the label reached the expert
path, was shlex-split into argv, and came back as `tm: error: argument <command>:
invalid choice: '🗓'` (observed 2026-09-02, all six buttons). A label arriving in expert
mode is therefore read as what it is — the athlete is looking at the companion — and
switches the persona back, confirmation and all, before the tap runs. Honouring the
label's argv while staying expert was the smaller change and the worse one: it leaves
the screen and the bot disagreeing about which persona is on, and leaves "💬 Talk to me"
with nowhere to arm. One rule instead: the keyboard you can see is the keyboard that
answers.

## 6. Simple rendering

Simple mode sets `TRAINMATE_RENDER=simple` in the subprocess env (beside
`TRAINMATE_FRONTEND=json`, and interpreted in one place like `is_json_frontend`).
Commands opt in one at a time; a command that hasn't opted in falls back to the expert
`<pre>` form — the web dashboard's lesson applied: a fallback that cannot decay beats a
parity promise nobody re-checks.

> **Superseded by DESIGN_render_persona.md.** The opt-in was a `cli/common.is_simple_render()`
> branch per surface, which reached fifteen sites across seven files. It is now a
> renderer object on `runtime.render`: `CompanionRenderer` extends `ExpertRenderer`,
> commands call one method per thing they have to say, and the override set *is* the
> opt-in list — so the fallback below is inheritance rather than discipline. Everything
> this section says about the *words* still holds; only where they live has moved
> (`cli/render.py`).

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
  vocabulary. One narrow amendment (2026-08-31, widened to a second
  surface 2026-09-02; DESIGN_runway_nudge.md §6): the runway button — on the morning push,
  and under the companion week view's end-of-schedule note — may send `workout generate`
  (or `workout generate -m ..<id>`) —
  a fixed argv from the CLI, never reachable through the router, and still gated on the
  preview/confirm pipeline. The one destructive action a tap can reach is `constraint rm <id>`, and
  only through the §5.5 picker: single ID, offered by the CLI, chosen by the athlete —
  never by the model. (Typed commands still work in simple mode, so the operator can
  drive an instance from its own chat when allowlisted there.) Amended 2026-09-01:
  the writes pass (§12) makes a bounded set of reversible-or-confirm-gated
  mutations routable and lets the model *nominate* (never execute) — §12.9 is now
  the authoritative statement of this posture.
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
| `trainmate/cli/render.py` | the companion voice: line builders, `ExpertRenderer`/`CompanionRenderer`, `TRAINMATE_RENDER` interpretation (was a helper in `cli/common.py` — DESIGN_render_persona.md §7) |
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
- Writes are routable through three shapes — view, picker, capture — and an
  operation fitting none belongs to the expert vocabulary (2026-09-01, §12).
  Capture is two router-model calls (route, then a domain-focused extraction);
  `add_constraint`/`add_signal` move off adapt onto `bot capture note`, with the
  adaptation offered as a button instead of ridden as a toll; edits nominate
  their object against CLI-given rows, previewed from the real row, tap-confirmed;
  goal delete means archive (`goal rm`, `--purge` unreachable from chat); settings
  are routable only over the §12.7 key allowlist. The capture family stays hidden
  under `tm bot` (option A) — promoting free-text authoring to public CLI flags
  (`constraint add -m`) is a possible later, purely additive step.
- Review amendments (2026-09-02, §12): every persisted capture ends with the adjust
  offer — signals included, because the athlete reporting one expects forward notice;
  the two lanes (free text talks to the app, 💬 Tell my coach talks to the coach
  verbatim) are taught by the arming line and help card; nomination is cross-domain
  (goals + upcoming sessions), human-plausible, with the coach hand-off for
  session-shaped asks and a pinned re-capture picker as fallback; extraction never
  fills a missing required field — it asks; the plan-shaping notice renders per
  persona; adapt's confirm loops and the candidate schema fragments are shared, not
  duplicated; a stale button tap says so; `add_goal` supersedes reply-only
  `new_goal`; disallowed setting keys are refused honestly, not routed to "unclear".

**Open**
1. Router echo: always show "→ …", or only when confidence is low? Draft: always;
   applies unless objected to before phase 3 (rollout §9). Implemented as: always
   (`ROUTER_ECHO` in trainmate_bot.py); trivially revisitable.
2. Whether the router model is enough for §12.2's extraction calls — transcription,
   not judgment, says yes; watched in practice, and the role is already a setting.
3. Multi-intent messages and slotted views (§12.8): deferred until a real message
   demands them.
4. Whether simple mode should keep the typed expert path open (raised 2026-09-02).
   Today every CLI command runs when typed with a leading `/` from an allowlisted
   chat (§7) — the router firewalls free text and buttons, not typing — which is how
   `--purge` stays technically reachable from the athlete's own chat (§12.6). An
   instance knob gating slash commands to operator chats would close that door; weigh
   it against the operator's §7 use of the same chat to drive an instance.

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

## 12. Writes: chat reaches operations (2026-09-01)

The router to date is a reader with one inbox: every intent either shows something or
drops the message into `adapt -m`. Three things it cannot do. It has **no slots** — the
model returns an intent name and nothing else, so "move my marathon to October 12" has
nowhere to put the date. It has **no way to name an object** — companion prose hides
IDs, so "I'm not doing the 10k" cannot safely become `goal rm 3`. And **recording a
rule rides the coach** — `add_constraint` pays for a full adaptation call on the
coaching model when all the athlete wanted was to be heard. This pass closes all three
without giving the model any new authority.

### 12.1 Three shapes, and the extension rule

Every routable operation takes exactly one of three shapes, and inherits its shape's
guardrails:

- **View** — intent → fixed argv from the bot's table. Exists (§5.3); unchanged.
- **Picker** — the CLI renders the list in companion prose and attaches a §4.4 button
  row whose leaves each carry a deterministic command with a real ID. The model picks
  *that* something should change; the athlete's tap picks *which*. Exists for
  `constraint rm` (§5.5); this pass adds a goals counterpart (§12.6).
- **Capture** — for anything that needs values from the message. A second,
  domain-focused LLM call extracts a typed proposal, the CLI previews it in companion
  prose rendered from real data, and a §4.4 prompt confirm makes it real. New (§12.2).

The rule that keeps the surface honest as it grows: **an operation that fits none of
the three shapes belongs to the expert vocabulary.** That is not a gap in the router;
it is the §7 line, restated as a design test. `plan generate`, wipes, model roles and
`restart` fail the test by construction — no shape gives the model authority over
anything plan-shaping, expensive, or irreversible.

### 12.2 Capture: two calls, both on the router model

`bot route` stays exactly as dumb as it is — one intent, no slots. A write intent then
runs a second hidden command, `tm bot capture <intent> "<text>"`, whose one LLM call is
domain-focused: it sees only the fields its intent can fill, plus today's date and
weekday so "next Friday" resolves, plus (for edits, §12.4) the current rows to nominate
from. Two small calls instead of one do-everything prompt, because the classifier's job
must not get harder every time a domain is added, and because read intents — the
overwhelming majority — keep paying for exactly one call.

Both calls run on the `router-model` role (§5.4). Extraction is transcription, not
coaching judgment, so the cheap model is the right default; whether it is *enough* is
an open question to watch (§10), and the escape hatch already exists — the role is a
setting.

**Extraction transcribes; it never fills** (2026-09-02, the signal path's
no-fabricated-numbers rule generalized to every capture, present and future). A
required field the message does not state — `add_goal` with no date is the canonical
case — comes back as a missing-field marker, never a plausible guess: a guessed date
in a preview is exactly what a confirm-tap sails past. The reply asks for the one
missing thing in one line ("When is it? Tell me again with the date and I'll set it
up."). *Resolving* is transcription and stays allowed — "next Friday" against the
given today, an underspecified "May 10" to its nearest future occurrence — and the
preview always shows the resolved absolute date, so a wrong year lands in front of
her eyes, not in the database.

Capture runs as an ordinary routed command: its questions are `TM-PROMPT` confirms, its
offers are `TM-BUTTONS` rows, its output is simple-rendered. Nothing new crosses the
CLI↔bot channel.

### 12.3 Notes move off the coach: `bot capture note`

`add_constraint` and `add_signal` stop mapping to `adapt -m` and map to one shared
`bot capture note` instead. Its extraction call returns the same candidate shapes the
adapt inbox yields — `new_constraints` and `new_signals` — and persists them through
the same confirmed-candidate paths (`capture_message_constraint`,
`capture_message_signal` in `coach/service/planning.py`), so the trust boundary moves
not at all: still advisory-only, still no LLM-set `rest` or `replan`, still no
fabricated numbers, still nothing stored without the athlete's yes. One capture call
extracts *both* kinds, so a mixed note — "knee's acting up, no running for two weeks,
and I slept terribly" — loses nothing by being routed here rather than to adapt.

The decoupling is the point: recording becomes instant and cheap, and the coach becomes
an offer. After a capture persists — constraint *or* signal (amended 2026-09-02) — the
reply carries a §4.4 button: "🔄 Adjust the plan around it" → `workout adapt`. Prompts
ask and block; buttons offer and exit (§4.4): she is heard immediately, and invoking
the coach is her call, not a toll. The first draft gave signals no offer — the record
points backward (§5.5) and adapts nothing — but the athlete *reporting* one usually
expects forward notice: "I slept terribly" filed as a row behind a cheerful confirm
reads as heard-and-acted-on while nothing about today changes. The offer makes that
gap one visible tap wide, and declining it is a non-action. When the extraction finds
no durable note at all, the reply says so gently and offers one button — "📨 Send it
to your coach as written" (`adapt -m` with the original text) — so a miss costs one
tap, not the message.

**The two lanes are taught, not discovered** (2026-09-02). The offer runs bare
`workout adapt`, so the coach reads the stored row, not the original words —
transcription is the price of the instant lane. The lane that carries her exact words
is 💬 Tell my coach (§5.2), and the surface teaches the difference instead of leaving
it obscure: the arming line becomes "I'm listening — I'll pass your words straight to
your coach.", and the help card names the lanes in athlete words — free text talks to
the app, which routes, records and offers; the button talks to your coach directly,
word for word. The router echoes carry the same lesson per message ("noting that rule
for your coach" vs "passing that on to your coach").

**One live button row per chat** is a §4.4 mechanic this pass turns into a constraint:
any new row — the morning push included — retires the pending one. An offer left
overnight is gone by breakfast, and a tap that finds its row stale must say so ("That
offer expired — just send it again.") rather than silently stripping the buttons: the
message behind a retired offer was already consumed by capture, so silence there loses
it twice.

**What §5.5's invariant becomes.** The note intents still share one argv and differ
only in their echo — a misroute between `add_constraint` and `add_signal` still changes
what the athlete is told, never what is stored, and `RouterTablesTest` keeps pinning
that. What changes is that `coach_message` now genuinely differs: state and
availability go to the coach, records go to capture. A misroute across *that* line
degrades gracefully in both directions — a rule misread as state still lands in adapt,
whose inbox still extracts it (just paying the coach call the athlete would have been
offered anyway); state misread as a rule is caught at the capture confirm, and the
no-find fallback's button walks it to the coach. The 💬 Tell-my-coach armed capture
(§5.2) stays verbatim-to-adapt: the explicit "just give this to the coach" path is the
standing escape hatch from routing altogether. From the terminal, `workout adapt -m`
is untouched — the CLI inbox keeps its extraction exactly as DESIGN_constraints.md §8
and DESIGN_signal_extraction.md §2 describe it.

### 12.4 Edits nominate their object

`edit_goal` and `edit_constraint` need an object *and* a value: "move my marathon to
October 12" names both. The capture call nominates the way a human assistant would
(reworked 2026-09-02): it sees the rows the athlete could plausibly mean — the active
goals or constraints of its own intent, *plus* the upcoming planned sessions (id,
title, date each) — because the athlete's nouns do not respect domain lines: "my long
run" names a session, "my marathon" names a goal, and only the data says which reading
is plausible this week. Nominating relaxes one clause of §7 ("which row is decided by
the athlete's tap, never by the model") into its load-bearing form:

> **The model may *nominate* an object, but the preview is rendered by the CLI from
> the real row, and nothing executes without the athlete's confirmation on it.**

The call returns the most plausible reading, and each kind lands differently:

- **A goal or constraint, nominated cleanly** — the ordinary preview/confirm.
- **A session** — the ask was coach territory all along: the preview offers the
  hand-off ("That sounds like Saturday's long run — shall I pass it to your coach?")
  and the confirm runs `adapt -m` with her original words. The wrong-domain picker
  this replaces — a list of goals answering a question about a session — never
  appears.
- **Several close candidates, or "no" on the preview** — a picker of the candidate
  rows. A leaf does *not* execute the edit: it re-runs `bot capture <intent> --id <n>
  "<text>"` with the nomination pinned, which re-enters the ordinary preview/confirm.
  The invariant above survives its own fallback — every path still ends at a
  CLI-rendered preview and a tap.
- **Nothing matches anywhere** — the §12.3 no-find treatment: a gentle miss and the
  send-to-coach button.

A wrong nomination shows the wrong goal in the preview — "Your goal **Marathon**
(Sat Oct 26) → move to **Sun Oct 12**. OK?" — and dies visibly on "no". The tap
remains the only thing that fires a write, and the argv it fires is assembled by the
CLI from the confirmed proposal, never authored by the model.

Chat-editable fields are the transcribable ones: a goal's title, target date and
description; a constraint's title, dates and description. Everything that changes an
object's *semantics tier* stays expert vocabulary — a goal's status, sports and
date-type; a constraint's `--rest` and `--replan` (the same trust boundary the capture
paths already enforce on add).

### 12.5 New goals: `bot capture add_goal`

"I want to run a half marathon on May 10" is a capture: title, date, sports (mapped
into `CANONICAL_SPORTS` by the extraction prompt, never free-typed into the enum), and
the event/horizon reading, previewed in the goal view's own on/"by ~" wording (§11) so
a wrong `date_type` guess is visible in the preview's first line. Confirm runs
`goal add`. A message with no date inherits §12.2's missing-field ask. The intent
*replaces* the reply-only `new_goal` the runway pass added (DESIGN_runway_nudge.md §6,
`NEW_GOAL_REPLY`): its honest "the operator sets this up" answer retires, the router
row renames to `add_goal`, and the runway tests pinning reply-only-ness move with it.
What it does *not* do is shape the plan: the reply closes with the §11
line — the plan appears once the coach lays it out — and `plan generate` remains the
operator's typed act. A goal row is cheap and editable; the periodization built on it
is neither, and stays behind the §7 line.

### 12.6 Calling a goal off: the goals picker

`remove_goal` mirrors `remove_constraint`: a hidden `tm bot goals` renders the active
goals in companion prose and attaches a picker whose leaves send `goal rm <id>`. Since
`goal rm` archives (the reversible-deletes pass; the hard cascade lives behind
`--purge`, which neither the router nor any button can reach — the typed `/` expert
path can, like every expert command, which is §10 open question 4), the one goal
mutation a tap fires is the
same reversible call-off `goal edit --status archived` performs — sessions stood down,
history kept, reinstatable by the operator. "I'm not doing the 10k anymore" *means*
calling it off; the athlete who truly wants a goal expunged is describing an operator
task. Archived goals then say nothing anywhere in companion mode (§11), which is the
tone rule doing the mourning. An empty active list renders the §11 invitation, never
a bare picker; a goal she means that is already completed or archived simply isn't
offered — the list she reads *is* the answer (already done, or already called off).

### 12.7 Settings: an allowlist, not a surface

"Can you message me at 7 instead?" earns `change_setting` → `bot capture
change_setting`. The extraction returns `{key, value}` where the key must come from the
**routable-keys allowlist** — `morning-time`, `morning-deadline`, `push` — the knobs
that shape the athlete's own experience of the chat. The allowlist is context given to
the extraction, and it is also enforced after: a key outside it (a model role,
`adapt-first`, anything operator- or cost-shaped) earns a one-line refusal that names
the boundary — "That one's for the operator, not me." — so "use a smarter model"
cannot become a settings write no matter what the extraction says. (2026-09-02: a
refusal, not the §5.3 unclear fallback — §5.3's whole argument is that misses should
be *visible*, and a boundary disguised as incomprehension teaches nothing.) Values
pass through the settings layer's own validation, and the preview reads back the
*effect*, not the key, naming the next real fire — the scheduler re-reads every tick
(§4.3), so a change made before today's push lands *today*, and "I'll open your day
at 07:00 from tomorrow. OK?" claims tomorrow only when today's is already past.
There is no `show_settings` view: the confirm echoes the value, and the full listing
is expert detail.

### 12.8 The intent table after this pass

| Intent | Shape | Runs |
|---|---|---|
| `show_today` `show_week` `show_goals` `show_plan` `show_progress` `show_constraints` | view | *(unchanged, §5.3/§11)* |
| `coach_message` | — | `adapt -m` *(unchanged — state should invoke the coach)* |
| `add_constraint` / `add_signal` | capture | `bot capture note` *(off adapt, §12.3)* |
| `edit_constraint` | capture+nominate | → `constraint edit <id> …` (§12.4) |
| `remove_constraint` | picker | `constraint rm <id>` *(unchanged)* |
| `add_goal` | capture | → `goal add …` (§12.5; replaces reply-only `new_goal`) |
| `edit_goal` | capture+nominate | → `goal edit <id> …` (§12.4) |
| `remove_goal` | picker | `goal rm <id>` — archives (§12.6) |
| `change_setting` | capture | → `settings set <key> <value>` (§12.7) |
| `help` / `unclear` | — | *(unchanged)* |

One intent per message stays the rule. A compound message is nearly always a
state dump, and `coach_message` → adapt handles those holistically — that is what
adapt is *for*; multi-intent routing is deferred until a real message demands it
(§10). The sanctioned next extension, when someone asks the router for a specific
day and gets this week, is a *slotted view* — `show_week` plus one validated date
selector — which is capture machinery applied to a read and needs no new rules.

### 12.9 Guardrails, restated

The §7 posture after this pass, in full:

- Routable mutations: `constraint rm`/`edit`, `goal add`/`edit`/`rm`(=archive),
  `settings set` over the §12.7 allowlist. Every one is reversible or confirm-gated;
  most are both. The hard deletes (`--purge`, wipes), plan-shaping (`plan generate`,
  `workout generate` outside the §7 runway button), model roles and `restart` remain
  typed expert vocabulary.
- The model never authors argv. It picks intents from a fixed table, fills typed
  fields the CLI validates, and may nominate an object or allowlisted key *from
  context the CLI gave it* — and none of that executes without the athlete confirming
  a preview the CLI rendered from real rows.
- Free text remains data, never instructions, at both calls; the blast radius of a
  hostile message is bounded by the argv tables and the allowlist, exactly as §7
  argued for the read-only router.

### 12.10 Touch points and rollout

| File | Change |
|---|---|
| `trainmate/cli/bot.py` | `bot capture <intent>` family (extraction prompts beside `ROUTER_SYSTEM_PROMPT`), `bot goals` picker, new `ROUTER_INTENTS` rows (`new_goal` → `add_goal`, §12.5) |
| `trainmate_bot.py` | new intent→argv and echo rows; text-carrying intents pass the message to `bot capture`; the stale-tap path speaks ("That offer expired — just send it again.", §12.3) instead of silently stripping the row |
| `trainmate/cli/workouts/generate.py` | the per-candidate confirm loops — the constraint confirm and `_confirm_new_signals` with its reuse-first category ladder — factor out into a shared helper `bot capture note` calls: one behavior, ladder included, on both paths |
| `trainmate/coach/engine/workouts.py` | the `new_constraints`/`new_signals` schema fragments and extraction-rule text become shared constants this prompt and the §12.2 capture prompts both include — one candidate vocabulary, no drift |
| `trainmate/coach/service/planning.py` | `capture_message_constraint`/`_signal` reused as-is, but the plan-shaping notice renders per persona: under simple rendering its `constraint edit --replan` / `plan generate` suggestion becomes the §12.3 adjust-offer button, never expert command text in companion chat (the personas abstraction in flight owns the split) |
| `trainmate/cli/settings.py` | routable-keys allowlist named beside the settings it guards |
| `tests/` | `RouterTablesTest` reshaped (note intents share `bot capture note`; every capture intent maps to `bot capture <intent>`), mocked-extraction tests per capture kind (missing-field marker included, §12.2), nomination outcomes per §12.4 (clean, session hand-off, pinned re-capture, no-match), the shared confirm helper exercised from both adapt and capture |

Rollout continues §9's numbering, each phase shipping alone:

4. **Note capture** — `add_constraint`/`add_signal` off adapt, the adjust-plan offer.
   The machinery foundation and the biggest felt win (recording becomes instant).
5. **Goal writes** — `add_goal`, `edit_goal`, the `bot goals` picker.
6. **Constraint edit** — capture+nominate over the §5.5 view's rows.
7. **Settings** — the allowlist capture.

Phase 5 depends on `goal rm` archiving (the reversible-deletes change, in flight on
`worktree-goal-rm-archives` as of this writing); the picker ships only on top of it.
