# Design: a Stop button while the coach is thinking

**Status:** Implemented · **Date:** 2026-09-09 · **Branch:**
`worktree-telegram-stop-button`

## 1. The problem

It is Wednesday evening. The athlete opens Telegram and types "knee is sore, move
tomorrow's run to Friday". The bot answers "Working on it — this usually takes about
40s." and goes quiet while the coach model writes the new session.

Ten seconds in they realise they meant *Saturday*, not Friday. There is an answer they
do not want on the way, and no way to stop it.

Two things are wrong today:

- **There is nothing to tap.** The only way to abort is to type `/cancel`, which is in
  the command menu but not in front of them at the moment they need it.
- **`/cancel` would not work anyway.** The bot stops fetching Telegram messages for the
  whole span where a command is computing with nothing to ask
  (`DESIGN_bot_restart.md` §5.1). That `/cancel` sits on Telegram's servers and is
  delivered only once the command it was meant to stop has finished. So: wait out the
  full 40 seconds, read the wrong answer, send the right message, wait 40 seconds
  more.

## 2. Goals / Non-goals

**Goals**

- One tap ends a command that is waiting on the coach, at the moment the athlete is
  looking at the wait.
- `/cancel` works again during that wait, for the same reason.

**Non-goals**

- Stopping the model's work at OpenRouter. The request is already in flight and will be
  billed; what stops is *this* bot waiting for it and acting on the answer.
- Undoing work the command already wrote before this call. See §6.
- A progress bar, a percentage, or a cancel-with-reason. One button, one meaning.

## 3. What the athlete sees

The "Working on it…" message grows one inline button:

```
Working on it — this usually takes about 40s.
[ ✋ Stop ]
```

Tapping it: the button disappears from that message, the bot replies "Stopped.", and
the command is over. Nothing else arrives.

When the coach answers first, the button disappears on its own — the answer is the
end of the wait, so a Stop offer under it would be a button for something that already
happened.

## 4. Where the button hangs, and why there is exactly one place

`openrouter.complete()` prints the wait notice and then emits the `TM-FLUSH` marker,
which is the bot's signal to close the current chat message
(`DESIGN_output_verbosity.md` §7.3 and §8.2). That marker is, by construction, the one
moment in the whole system where a command is about to go quiet for the coach: every
LLM command passes through that single call site.

So the bot attaches the Stop button to the message that flush just sent. No new
sentinel, no new protocol field, and no list of "commands that are slow" to keep in
sync. The wait notice is printed *before* the flush (a test in `test_openrouter.py`
locks that order), so the flushed message is never empty and there is always something
to hang the button on.

The two calls that pass `wait_notice=False` — `bot route` and `bot capture`
(`DESIGN_output_verbosity.md` §8.4) — run in subprocesses whose stdout the bot captures
and throws away. They never reach this path, which is right: they take a second or two
and nobody is watching them.

## 5. The polling model changes

This is the enabling change, and it reverses a decision recorded in
`DESIGN_bot_restart.md` §5.1 and §7. That design had the bot fetch Telegram updates in
three different ways depending on what it was doing:

| State | Polling was | Polling is now |
|---|---|---|
| Idle, no command running | live | live |
| A prompt is open, waiting on the athlete | live | live |
| Computing, nothing to ask | **paused** | **live** |

The pause was a simplification, not a safety requirement: with one command in flight
per chat there was nothing useful to receive, so the bot did not listen. Its accepted
cost was written down at the time — "`/cancel` can no longer interrupt a command that's
actually stuck computing" — and this design is the revisit that paragraph invited.

Listening during compute is safe, because the bot already knows what to do with every
kind of update that can arrive while a command is running. That logic exists today and
is unchanged; the pause only meant it ran a few seconds later than the tap.

- A **Stop tap** — §3.
- A **new command** ("show my week") — answered "A command is still running. Use the
  buttons above, or /cancel."
- **`/cancel`** — kills the subprocess. This is the pre-`/restart` behaviour, restored.
- **`/restart`** — tears down whatever session is live, computing or not. The restart
  design already handles the mid-compute case (§5.2 steps 1-2); it called that case
  rare, and it is now ordinary.

Removing the pause removes code: `_resume_polling`, the `restarting` latch that existed
only to stop `_drive` reopening the long-poll behind a `/restart`, and the pause/resume
calls threaded through `_drive`. What stays is `_pause_polling`, which `/restart` still
uses to close the long-poll cleanly before it exits — that part of §5.1's machinery was
never about mid-session pausing (`DESIGN_bot_restart.md` §7, the `getUpdates` offset).

## 6. What "Stopped." does and does not claim

Stopping kills the CLI subprocess, exactly as `/cancel` does. That is a hard kill, so:

- The coach's answer never lands. Whatever this call was about to write is not written.
- Work the command already finished *before* this call stays. `data pull` writes the
  activities it downloaded and then asks the coach to reflect on them; stopping the
  reflection does not un-download the activities.

The reply is therefore the single word "Stopped.", which is true in both cases. It does
not say "nothing was saved", because for a multi-step command that would be a lie, and
a caveat the athlete has to learn to distrust is worse than no caveat.

## 7. One live button, and taps that arrive late

The button's `callback_data` is `stop:{nonce}`, where the nonce is the random token the
bot already mints per command (`_Session.nonce`, used the same way for prompt answers).
A tap is honoured only while that exact command is still running.

Two cases fall out of that, both wanted:

- **A command that queries the coach twice** — `plan generate` for two goals asks the
  coach once per goal, and a first-ever run does `data bootstrap` before it. The second
  wait raises its own button and the first is retired, so there is one live Stop in the
  chat. Either would have worked; both name the same command.
- **A tap on a button left over from a finished command** — the row is dropped and the
  athlete is told "That's already finished — nothing to stop." A late tap must never
  reach whatever is running *now*, which is why the nonce is in the data rather than
  just "stop the current command".

## 8. Decisions

- **The button is attached to the wait notice, not sent as its own message.** A second
  message per LLM call would double the chat noise of every command for one button.
- **No confirmation step.** Stopping is cheap and the athlete tapped a button labelled
  Stop. A "are you sure?" prompt on an abort is the wrong shape: the expensive action is
  the one already running.
- **Expert and simple mode get the same button.** The wait is the same wait, and a
  difference between the two surfaces that cannot be explained in one sentence is a
  design bug (AGENTS.md).
- **The `command_timeout` watchdog stays.** It bounds a run nobody is watching — a
  morning push, or a wait the athlete walked away from. Stop is for the athlete who is
  looking at the screen; the watchdog is for the one who is not.
