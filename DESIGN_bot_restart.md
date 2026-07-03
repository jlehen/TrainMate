# Design: Bot Self-Restart (`/restart`)

**Status:** Proposed · **Date:** 2026-07-03 · **Branch:** main

## 1. Motivation

`tm-bot` is a thin wrapper that `exec`s `trainmate_bot.py`, a single long-polling
process with no supervisor. Today, picking up a new deploy, or nudging a bot
that's gone unresponsive without it having actually died, means someone with
shell access has to kill and relaunch it by hand. The athlete wants to trigger
that from Telegram itself with a `/restart` command, without adding a second
binary/script to keep in sync with `tm-bot`.

Note the scope this implies: `/restart` only helps when the bot is still
responsive enough to *receive* it. A genuine hang (deadlocked, not polling at
all) or a crash (process is gone) can't be recovered via a Telegram command by
definition — those still need shell access or external supervision, and this
design doesn't attempt to change that (§2 Non-Goals).

## 2. Goals / Non-Goals

**Goals**
- An authorized chat can send `/restart` and get a fresh bot process back within a
  few seconds, no shell access required.
- Reuse `tm-bot` as the only file that changes shape — no new script to deploy.

**Non-goals**
- Zero-downtime / hot code reload. A brief gap while polling tears down and
  restarts is acceptable.
- Automatic crash recovery. A worker that exits for any reason other than the
  deliberate restart signal (unhandled exception, OOM-kill, a stray SIGTERM,
  whatever) is **not** relaunched — the supervisor exits right along with it,
  exactly like unsupervised `tm-bot` behaves today (§4). Recovering from a real
  crash still means a human notices and restarts it (shell access, or whatever
  the deployment layer provides) — `/restart` can't help, since a crashed process
  isn't around to receive it. Traded away for a much simpler supervisor: no
  backoff schedule, no crash-loop, no "was that a deliberate stop or a crash?"
  judgment call.
- Supervision across host reboots (systemd/tmux/whatever runs `tm-bot` today) —
  unchanged.
- Restarting other TrainMate processes (`trainmate_web.py`, cron jobs) from
  Telegram — `/restart` only targets the bot.
- Multi-chat considerations. TrainMate's config only supports one athlete /
  one allowlisted chat right now, so this design doesn't reason about
  concurrent sessions across chats.

## 3. Architecture: supervisor loop + child sentinel

`tm-bot` gains two modes, selected by an env var it sets on itself rather than a
CLI arg — a positional/flag arg would risk colliding with args meant to be passed
through, and env vars naturally aren't inherited by anything that isn't this
script's own child.

- **Default invocation** (`./tm-bot ...`, e.g. from a terminal, tmux pane, or
  systemd unit): `tm-bot` is the **supervisor**. It loops forever, each iteration
  launching a child of itself with `TM_BOT_SUPERVISED=1` set and `"$@"` forwarded,
  then waits for it to exit and decides whether to relaunch (§4).
- **`TM_BOT_SUPERVISED=1` already set**: `tm-bot` is the **worker** — it skips
  straight to today's venv-check-and-`exec` of `trainmate_bot.py`. This path is
  byte-for-byte what `tm-bot` does today.

`trainmate_bot.py` doesn't need to know any of this exists — it only needs to
exit with the right code when a restart is wanted (§5).

## 4. Exit-code contract

Simplified to one special case:

| Outcome | Trigger | Supervisor action |
|---|---|---|
| Requested restart | worker exits with `RESTART_EXIT_CODE` (75) | relaunch immediately |
| Everything else | any other exit — code 0, a crash, a signal-induced death, operator Ctrl-C, whatever | supervisor exits too — no relaunch |

Concretely:

- Reserve an exit code for "please restart me" — `RESTART_EXIT_CODE = 75`
  (arbitrary, borrowed from `EX_TEMPFAIL` in `sysexits.h`, just needs to be
  documented and not collide with Python's own exit code for uncaught
  exceptions, which is 1).
- Because only 75 relaunches, there's no backoff schedule, no crash-loop, and no
  need to classify a child's exit as "deliberate stop" vs. "crash" — every
  non-75 exit gets the same treatment (the supervisor also exits). This is what
  makes automatic crash recovery explicitly out of scope (§2): a crash now
  behaves exactly like it does in today's unsupervised `tm-bot`.
- The supervisor still **traps SIGINT/SIGTERM sent to itself**, but only to
  avoid orphaning the child — not to decide whether to relaunch (the exit-code
  check already does that). Two cases matter differently:
  - A signal delivered to the whole foreground process group (Ctrl-C at a
    terminal) reaches supervisor and child independently; the child dies on its
    own and the supervisor, left untrapped, would default to terminating too —
    so in this case the trap isn't strictly required for correctness, just for
    a tidy log line.
  - A signal sent to *just* the supervisor's PID (e.g. `kill <pid>`, or
    `systemctl stop` under `KillMode=process`) does **not** automatically reach
    the child. Without a trap, the supervisor would die and leave the worker
    running, unsupervised and undiscoverable by name (see the open question in
    §7 on how `tm-bot` is actually run). The trap should signal the child, wait
    briefly, `SIGKILL` if it's still alive, then let the supervisor exit.
- Implementation note: `tm-bot` currently has `set -e` at the top. Capturing the
  child's exit status with a bare `wait "$pid"` trips `errexit` on any non-zero
  status — which is every relaunch-worthy exit, including 75 — and would abort
  the supervisor before it ever checks the code. Capture it as
  `wait "$pid" || rc=$?` (or inside an `if`) so `set -e` doesn't short-circuit
  the loop.
- Supervisor lifecycle events (start / stop / relaunch + reason) get logged to
  stdout, same stream `tm-bot` already writes to today.

## 5. Command surface

### 5.1 Polling model

To keep this simple with a single authorized chat (§2), `tm-bot` only polls
Telegram (`getUpdates`) while there's nothing to compute: idle, or blocked on
the athlete's answer to an open prompt. It stops polling for the span where a
CLI subprocess is silently churning with no prompt open.

This has to be scoped to the *silent-compute* phase specifically, not the whole
session — the existing interactive-prompt protocol (`_present_prompt`,
`trainmate_bot.py:270-301`) depends on live polling to receive the athlete's
button tap or text reply while the subprocess is blocked on stdin. Pausing
polling for the entire session lifetime would silently break every confirm/choose
prompt (plan apply, destructive-command confirmation, etc.) — the bot would never
see the answer. So the three states behave differently:

| State | Polling | Rationale |
|---|---|---|
| Idle (no session) | live | need to receive the next command |
| Prompt open (subprocess blocked on stdin awaiting the athlete) | live | need to receive the tap/reply |
| Silently computing (subprocess running, no prompt open) | paused | nothing meaningful can be sent to it anyway |

Telegram's `getUpdates` offset mechanism means pausing doesn't drop anything —
the server holds undelivered updates and they arrive, in order, the next time
polling resumes. But it does mean **`/cancel`'s existing behavior changes**:
today it kills a mid-compute subprocess instantly; under this model, a `/cancel`
sent during the silent-compute phase just queues and only takes effect once the
subprocess finishes on its own (transitions to idle or opens a prompt) and
polling resumes — i.e. `/cancel` can no longer interrupt a command that's
actually stuck computing, only one that's idle-waiting on the athlete. **Open
question, not just an implementation detail**: is that an acceptable change to
already-shipped `/cancel` behavior? Flagging explicitly since it wasn't the
question being asked when this trade was made (§7).

Implementation-wise this likely means replacing `application.run_polling()`'s
always-on background fetch loop with an explicit start/stop around the compute
phase (or a manual `get_updates()` loop) — `run_polling()` doesn't expose a
"pause between messages" toggle, so this is more than a branch added to
`on_message()`; treat it as a real touch point in `trainmate_bot.py`'s core loop,
not a side effect of adding `/restart`.

### 5.2 `/restart`

Handled the same way `/cancel` and `/start` already are in `on_message()`
(`trainmate_bot.py:398-404`) — matched on `token_low == "restart"`, gated by the
existing `is_authorized(chat.id, allowed_ids)` check. No new auth mechanism: the
Telegram allowlist is already the access control, so there's no need for a
shared secret at this layer (that idea only makes sense at the process layer in
§3, where it's not doing security work either — it's just avoiding argv
collisions).

Because of §5.1's polling model, `/restart` can only ever be *received* while
idle or while a prompt is open — never while a subprocess is silently
computing, since polling is paused for that whole span. So the only cleanup
`/restart` itself needs to do is for the prompt-open case:

Behavior:
1. If a prompt is currently open for this chat (subprocess blocked on stdin
   awaiting the athlete's tap/reply), kill it first — same as `_cancel`'s
   mid-prompt path — so the child `tm` process isn't left blocked forever. (If
   idle, there's nothing to clean up.)
2. Reply "Restarting…".
3. `os._exit(RESTART_EXIT_CODE)` — a hard exit rather than trying to unwind
   `application.run_polling()` cleanly from inside a handler.

## 6. Touch points

- **`tm-bot`**: add the supervisor loop, the `TM_BOT_SUPERVISED` branch, and the
  signal trap (§4). No backoff logic needed. The existing venv-bootstrap + exec
  becomes the worker body, untouched.
- **`trainmate_bot.py`**:
  - `RESTART_EXIT_CODE = 75` constant.
  - `on_message()`: new `token_low == "restart"` branch (§5.2).
  - Polling loop rework to pause/resume `getUpdates` around the silent-compute
    phase (§5.1) — the larger of the two `trainmate_bot.py` changes, and one
    that touches existing `/cancel` behavior, not just new code.
- No config changes — reuses `telegram.allowed_chat_ids`.
- No DB changes.

## 7. Decisions & Open Questions

- **Only `RESTART_EXIT_CODE` relaunches; every other exit ends the supervisor
  too.** Locked. Simpler than distinguishing "deliberate stop" from "crash," at
  the cost of automatic crash recovery (§2 Non-Goals).
- **Polling pauses only during silent compute, not for the whole session**
  (§5.1). Locked — a session-wide pause would break the interactive-prompt
  protocol.
- **`/cancel` no longer interrupts silent compute instantly** — it now queues
  behind polling being paused, same as `/restart`. **Open:** confirm this is an
  acceptable behavior change to an already-shipped command; it wasn't the
  original ask, it fell out of the polling-model simplification (§5.1).
- **`/restart` kills only an open prompt's subprocess**, not a silently-computing
  one — it can't be received during silent compute at all under the new polling
  model (§5.2).
- **Open:** does python-telegram-bot drop or replay updates that arrive during
  the *restart* gap specifically (full process handoff, not the pause/resume
  within one process)? `run_polling`'s update-offset tracking is in-memory only
  and doesn't survive the process exiting, so it matters whether messages sent
  in the ~1-2s gap get delivered once the new worker starts polling or are
  silently missed — and separately, whether the new worker's first `getUpdates`
  can race the old connection's teardown and hit a `409 Conflict` (Telegram
  allows only one long-poll per bot token). Needs a quick check against the
  installed PTB version before this ships; if updates drop, worth a one-line
  warning in the `/restart` reply.
- **Open:** how is `tm-bot` actually run in production right now (bare
  foreground, `nohup`, `tmux`, a systemd unit)? Determines whether a plain
  `kill <supervisor-pid>` reaches the child automatically (process-group
  delivery) or needs the supervisor's trap to relay it (§4), and whether the
  supervisor's own stdout needs explicit redirection it doesn't have today.

## 8. Out of Scope

- Hot-reloading code without a process restart.
- Automatic crash recovery / crash-loop backoff (§2).
- Restarting `trainmate_web.py` or other services from Telegram.
- Multi-instance / highly-available bot deployment.
- Multi-chat / concurrent-session handling (§2).
