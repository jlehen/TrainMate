# Logging: a run journal, beside the LLM exchange log

## 1. The problem

TrainMate already logs one thing very well. Every call to the model is written to
`logs/llm_exchanges/<timestamp>_<label>.md` with the full system prompt, the full user
prompt and the raw response. Three months of use has produced 218 of those files and
17 MB on disk.

None of them can answer any of the three questions I actually ask.

**"What happened at 6:15 this morning?"** The Telegram bot fires `tm bot morning` on a
timer. Nobody is watching. The command's output goes to a phone, gets read once, and
scrolls away. If it did something odd — briefed the wrong day, silently skipped the
Garmin pull, took ninety seconds — there is nothing left to look at. There is an
exchange file from 06:15, but it does not say which command produced it, whether that
command succeeded, or what the app did with the answer.

**"When did the Garmin sync start failing?"** `garmin/sync.py` catches a failed pull and
prints one yellow line — `Garmin sync failed (...). Continuing with cached data.` — and
carries on with stale data, which is the right behaviour. On a terminal you see it. Over
Telegram you probably do not, because it arrives above the answer you asked for. Either
way it is gone the moment the screen is redrawn. An expired Garmin token can degrade
every reading for a week before anyone notices, and afterwards there is no way to find
out when it started.

**"What has this cost me, and where does the time go?"** Token counts exist — one line
inside each of the 218 markdown files. There is nothing to add up. I cannot say what a
month costs, which command spends the most, or whether the model I switched to last week
is slower than the one before it.

The gap is not that the app records too little. It is that what it records is a pile of
documents with no spine. Nothing says *this run happened, this is what it did, this is
how it ended*.

## 2. What belongs in a log here, and what does not

Two rules decide this, and they are worth stating before any mechanism, because the usual
failure of a logging system is that it logs the wrong things thoroughly.

### 2.1 The lifecycle rule

> If the athlete would miss it in a year, it is a database row, not a log line.

TrainMate already records a great deal of history: `workout_changes` says how a session
was modified and why, `plan_feedback` holds the athlete's notes, `coach_learnings` and
`learning_evidence` hold what the coach concluded and from what, `macrocycles` keeps every
plan version. That is the **domain record**. It is permanent, it is backed up with the
database, and it is read by the app itself.

The journal is the **operational record**: which process ran, when, for how long, what it
called out to, what it decided to skip, how it ended. It is disposable after a few months.

It is read back in exactly one place, and the exception is worth stating because it is
the shape any future one has to keep. `journal.llm_durations()` reads past `llm.call`
durations so a command can say how long it is about to take
(`DESIGN_output_verbosity.md` §8). That stays inside the rule above: the answer is a
courtesy the athlete forgets in a minute, it is *about* runs rather than about training,
and losing the whole directory costs the estimate and nothing else. A read that the app
would be wrong without belongs in a table.

Different lifetimes mean different homes. Nothing that already has a table gets copied
into the journal, and nothing in the journal is allowed to become the only copy of
something the coach needs.

### 2.2 The audience rule

> Output is for the athlete, now. The journal is for the operator, later.

They are the same person, but not in the same moment, and they want different things.
`DESIGN_output_verbosity.md` §3 already sorted every printed line into three tiers —
the answer, a warning or error, an aside — on exactly this basis: whether a line is still
true by the time it arrives. That work does most of the classification the journal needs,
and §5 below reuses it rather than inventing a second scheme.

The tier the output design has no name for is the one that matters most here: **things the
app must not say to the athlete but must not forget either.** There are ten of them today,
written as `except Exception: pass` — three in `garmin/client.py`, seven in
`trainmate_bot.py`. A failed `edit_message_reply_markup` is genuinely not the athlete's
problem, so staying silent on screen is correct. Staying silent on disk is not.

## 3. The unit is the run, not the line

A log of independent lines is a log you grep. A log of runs is a log you *read*.

A **run** is one command from parse to exit. It gets an id — 8 hex characters, shown and
matched git-style so a unique prefix is enough — and every event the command produces
carries that id. Two records bracket it:

- `run.start` — argv, source, pid, front-end, parent run, config path, database path.
- `run.end` — `outcome`, exit code, wall-clock milliseconds, and a rollup: number of LLM
  calls, total tokens, how many `warn` and `error` records the run wrote, and the error
  itself if there was one.

The model is deliberately *not* on `run.start`. Reading it means
`llm_models.active_model()`, which reads a setting, which builds the `Database` — the
thing §4 goes out of its way not to do at run start. It also is not known yet: the
`--llm-model` override is applied a few lines later. It belongs on `llm.call`, which is
the only place it is ever actually true.

**Three outcomes, because exit codes conflate three things.** `ok` covers a command that
finished, *including* a domain refusal ("no active plan"). `cancelled` is
`PromptCancelled` — the front-end `/cancel` or an idle timeout — which exits 130 today and
is explicitly not a failure. `failed` is an unhandled exception. Without this, `--failed`
would list every command that correctly answered no.

**A line that never became a command is not a run.** `tm benchmark record` is missing a
required argument, so argparse prints that command's help and exits 2. Nothing happened.
The same goes for `tm goal` with no sub-command, for bare `tm`, and for every `-h`. Those
were a fifth of the runs in the journal's first ten days, and they answer no question
anyone asks of a log — worse, the parse never reached `name_run`, so they are exactly the
runs §7.1 cannot classify and therefore refuses to hide.

So they are not written at all. The bracket still opens before the parse, because a crash
*inside* the parse is still a run; what changes is that `run.start` is built there and
held in memory, and a `UsageExit` — a `SystemExit` subclass raised by the single `exit()`
every parser in the tree inherits, so one override covers `-h`, `--helpall` and every
`error()` — drops the run rather than ending it, and nothing reaches the file.

This is the one record in the journal that is written late, and the moment is chosen so
that costs nothing. `name_run` writes it: the first thing after a successful parse and
before any database or network work, so a run killed anywhere it could actually be killed
still left a `run.start` behind and still reads as `?`. A drop arriving after the start
was written closes the run normally instead — a start with no end means *killed*, and
faking one of those would be worse than the noise this removes.

**The list view reads the two bracket records and nothing else.** `run.start` supplies the
id, the time, the source and the command; `run.end` supplies the duration, the rollup and
the outcome. It never has to scan a run's events to summarise it. A `run.start` with no
matching `run.end` is a run that is still going or one that died without a chance to
write — a `SIGKILL` from the bot's watchdog, a reboot mid-`plan generate`. Those show as
`?`, which is the row you most want to see and the one a `run.end`-only listing would
have hidden.

**Where the bracket goes: `run_once`, not `main`.** `tm shell` runs many commands in one
process, so a run is a command, not a process. `run_once` in `trainmate_cli.py` is the one
function both `main` and the REPL call, so bracketing there covers both surfaces with a
single edit and leaves the two existing error boundaries exactly as they are — the bracket
records the exception and re-raises it.

**Runs have parents, and `tm shell` is one.** `run_once` is re-entrant: it handles the
`shell` command by calling `_repl`, which calls `run_once` for every line typed. So three
lines in a shell session produce four runs — the shell, and one per line naming the shell
as its parent. That is the right reading of what happened, and it is the same shape as
the bot: the bot spawns the CLI as a subprocess and passes its own run id down in
`TRAINMATE_PARENT_RUN`, which the child records on `run.start`. The morning push — the bot
deciding to fire, the subprocess it launched, and everything that subprocess did — reads
as one story instead of two disconnected halves.

**How a deep call site knows its run.** `journal.py` keeps the open runs in a module-level
list; the current run is the last entry, pushed at `run.start` and popped at `run.end`.
Nesting only ever happens in `tm shell`, and every surface that writes is single-threaded
by construction (the CLI, the REPL, the bot's event loop), so a plain list is enough and a
`contextvar` is not needed. The web app never opens a run at all (§13, Phase 3).

**`source`** says who started it: `cli`, `repl`, `shell`, `bot`, `push`, `route`, `web`,
`test`. This is the field that makes "show me only what ran while I was asleep" a one-flag
query. The bot passes it in `TRAINMATE_SOURCE` — but note that `_cli_env` is one function
shared by three callers with three different answers, so it takes the value as a
parameter rather than setting a constant beside `TRAINMATE_FRONTEND`: `_start_command` for
a chat message is `bot`, the same function firing the morning push is `push`, and
`_route_intent` is `route`. Keeping the router separate matters because it runs once per
free-text message and is pure noise in every other view.

## 4. Where it lives

`logs/runs/YYYY-MM-DD.jsonl` — one file per day, one JSON object per line.

**The day is UTC**, and so is every `ts` inside the records: ISO with milliseconds. That
is not this app's usual answer, and the first draft of this design said the athlete's
calendar day, from `clock.active_zone()`, because every other date is resolved that way
(`DESIGN_user_timezone.md` §1).

It cannot be. `active_zone()` reads the `timezone` setting, which reaches
`from trainmate.db import db`, which builds a `Database`, which runs all twenty schema
migrations. Naming a log file would create and migrate the database on `tm help` — the
exact thing `runtime.py` and `trainmate/db/__init__.py` both carry docstrings forbidding.
Worse, it would put a database dependency in the one code path whose job is to survive the
database being unreachable, which is most of §4.1's argument.

So the file name is the one date in this app that is not the athlete's: it comes from the
system clock and nothing else. Writing a journal line reads the config and touches the
filesystem, and does nothing else at all.

The athlete's day comes back at display time, where every stored timestamp already gets it
(§5 of the timezone design): `tm journal` converts each record's `ts` through the athlete's
zone, so "what happened on Tuesday" is still answered in local terms. A local Tuesday can
straddle two UTC files, so the reader opens the window it needs plus one file either side
and filters on the converted timestamps. At a few thousand lines a month that costs
nothing.

The file is chosen by each event's own timestamp, so a long run has its tail in the next
day's file. The reader stitches a run back together by id, so this costs nothing to read
either.

### 4.1 Why a file and not a table

The obvious alternative is two SQLite tables next to the other nineteen. Three reasons
against:

- **A log inside the thing it watches has a blind spot exactly where you need it.** "The
  database is locked", "the schema migration failed", "the config names a database that
  does not exist" are the events most worth having, and a database-backed log cannot
  write any of them. (This is also the second reason for the UTC file name above: a
  journal that had to ask the database what day it was would have the same blind spot,
  one level down.)
- **Crash safety.** An interrupted command rolls its transaction back and takes the
  trace with it. An append-only file keeps every event written before the kill, which is
  the whole point of a post-mortem.
- **Different lifetimes, different homes** (§2.1). Retention is `rm logs/runs/2026-05-*`,
  not a `DELETE` and a `VACUUM` against the file that holds the training history.

The queries the journal has to answer are small — a single athlete produces a few thousand
lines a month — so reading and aggregating them in Python is instant. If that ever stops
being true, JSONL loads into a temporary SQLite table in ten lines, and that decision can
be made then rather than now.

### 4.2 The record

```json
{"ts":"2026-08-26T04:15:02.184Z","run":"a3f91c2e","seq":7,"lvl":"info",
 "ev":"llm.call","msg":"workout_adaptation via anthropic/claude-opus-4",
 "d":{"model":"anthropic/claude-opus-4","tokens":38104,"ms":24118,"ok":true,
      "file":"logs/llm_exchanges/20260826_061502_184213_a3f91c2e_workout_adapt.md"}}
```

| field | what it is |
| --- | --- |
| `ts` | UTC ISO, milliseconds. |
| `run` | The run id. |
| `seq` | Counter within the run, from 0. |
| `lvl` | `debug`, `info`, `warn`, `error`. |
| `ev` | What happened, from a closed vocabulary (below). This is what you filter on. |
| `msg` | The human sentence. This is what you read. |
| `d` | Optional structured fields. Absent when there are none. |

`seq` earns its place because three surfaces append to one file: two runs interleave, and
`seq` puts one run's events back in the order they happened, exactly.

There are four levels because §5 has four sources and no fifth.

`ev` and `msg` are both there on purpose. `ev` is a machine handle that must stay stable;
`msg` is prose already written for a human, which is most of why §5 costs so little.

The vocabulary is deliberately short — nine names, and adding one should feel like a
decision:

`run.start` · `run.end` · `note` · `llm.call` · `garmin.pull` · `calendar.write` ·
`db.write` · `bot.event` · `internal`

A warning is not its own event name. It is `note` with `"lvl":"warn"`. Severity and
subject are separate axes, and collapsing them produces a vocabulary that grows forever.

The cost of a vocabulary this short, stated plainly: all 29 reclassified asides (§5.2)
land on `note`, so `ev` is a coarse filter for exactly the events there are most of. The
alternative — `garmin.skip`, `plan.reuse`, `date.default` — is the vocabulary sprawl this
list exists to prevent, and `msg` is grep-able prose. If one subject turns out to be worth
filtering on repeatedly, it earns a name then.

### 4.3 Writing a line

One event is one line is one `os.write()` on a descriptor opened `O_APPEND`. On Linux
that single call is atomic against other appenders — the offset lookup and the write
happen under the inode lock — so the CLI, the bot and the web app can share the file with
no lock of our own. Two rules keep it that way:

- **Never buffer in user space.** Python's buffered writer can split a large string into
  several `write()` calls, which is exactly how two processes interleave inside one line.
- **Bound the line.** Records are truncated to 8 KB. Of that, a traceback gets a 4 KB
  budget: its first 1 KB and its last 3 KB with an elision marker between, because the
  innermost frames are the ones that matter and Python prints them last. The bound is
  also what keeps the single-`write()` assumption honest — a short write on a local file
  at this size does not happen, and a *retry* after one would be precisely the
  interleaving the single call is buying us.

**The reader skips any line that is not valid JSON.** That is one line of code and it
covers everything the writer cannot promise: a torn line from a power cut, a truncated
tail, a file someone edited by hand. There is no `fsync` — a process crash still leaves
everything already written, because it is in the kernel's hands by then; only a power cut
loses the tail, and the reader shrugs at it.

**The journal never raises.** Every write is wrapped, and a failure prints one warning per
process to stderr and then goes quiet. That warning is gated on `asides_enabled()`, which
is not fussiness: the bot spawns the CLI with `stderr=STDOUT` and streams that buffer
straight into the chat, so an ungated warning about the log file would arrive on the
athlete's phone — the one thing §2.2 says must never happen. A command must not die
because its log could not be written; the existing `_log_exchange` already takes this
position and it is the right one.

### 4.4 What is never written

- The OpenRouter API key, or any value from `config.llm.api_key`.
- Full prompts and full responses. They already have a home, and §6 links to it.
- Anything derived from the athlete's free text beyond what argv already contains.

The journal is as sensitive as the database — `workout adapt -m "hungover from the
wedding"` lands in a `run.start` record — and it lives in the same gitignored `logs/`
directory. Nothing in this design sends it anywhere.

## 5. How events get written without two hundred new call sites

This is where a logging design usually goes wrong: it adds a call at every interesting
place, the calls drift out of step with the code, and two years later the log describes an
app that no longer exists. The way out is to not add call sites at all, but to give the
ones that already exist a second job.

### 5.1 Four verbs

| verb | prints | journals | what it means |
| --- | --- | --- | --- |
| `print(...)` | always | no | The answer. Unchanged. |
| `aside(msg)` | terminal only | no | A hint or a standing caveat. Unchanged. |
| `step(msg)` | terminal only | `info` | What the app is doing right now. **New.** |
| `warn(msg)` | always | `warn` | Changes what the answer means. **New.** |
| `fail(msg)` | always | `error` | The command could not do its job. **New.** |
| `journal.debug(ev, **d)` | never | `debug` | What must not reach the athlete. **New.** |

Read down the "journals" column and the log's shape is just the output design plus the one
tier the athlete never sees.

These are all things the app says. The one thing it hears — the answer to a yes/no
question — is recorded in the prompt broker rather than by a verb, and §5.6 says why.

### 5.2 Splitting `aside`

`DESIGN_output_verbosity.md` put two different things in the aside tier, because for the
purpose it had — should this print on a phone? — they behave identically. The journal
needs them apart. Of the 44 `aside()` calls in the tree today:

**29 are trace.** They say what the app is doing or what it just decided:
`Auto-syncing Garmin 2026-08-22..2026-08-24...`, `Querying OpenRouter with model: X`,
`Garmin data is fresh (26m ago); using cache`, `Evidence unchanged since last analysis;
reusing cached reconstruction`, `Reusing existing periodization strategy from database`,
`No date given — adapting today (2026-08-26 Wed)`, `Router failed: {e}`,
`[2026-08-24] sleep data unavailable: {e}`.

Every one of those is a sentence a post-mortem wants. Several are decisions the athlete
can currently only infer — that the plan was reused rather than regenerated, that the
sync was skipped as fresh, that a per-day Garmin field came back empty.

**15 are hints and standing caveats.** `If you applied the new plan, run 'workout
generate'`, `Undo one with 'workout rollback'`, `PMC_TSB_LAG_NOTE`, `NEVER_SUM_NOTE`,
`Re-run with --debug for the full traceback`.

Those are advice to a reader. Logging "you could now run workout generate" tells a
post-mortem nothing at all.

So: the 29 become `step()`, the 15 stay `aside()`. `step()` prints through the same gate
and the same dim styling as `aside()` does today, so **no screen changes**. The journal
fills itself from lines that were already written, already worded for a human, and already
sitting at the moment worth recording.

Three aside-tier decisions do not go through `aside()` at all and are untouched here:
`cli/progress.py` and `cli/status.py` each call `asides_enabled()` directly to decide
whether to build `intensity.format_notes`, and `data pull` returns its summary as a string
for the caller to print (§3.1 of the output design). All three are caveats or answers, not
trace, so the trace tier loses nothing by not reaching them.

The naming follows the precedent the output design set when it chose `aside` over `note`:
pick a word this app does not already spend four ways. `step` is free; `trace` and `log`
are not, since `log` already means the athlete's training log
(`ARCHITECTURE.md` §15, "Workouts are a log").

### 5.3 `warn` and `fail`, and the line they draw

There are 178 `print(yellow(...))` and `print(red(...))` calls. Almost all of them stay
exactly as they are, and that is the interesting part.

A **domain refusal** is an answer: "no active plan", "that goal is in the past", "nothing
scheduled for Thursday". It is the app correctly telling the athlete about their own data.
It is not an event.

An **operational warning** is a log line: "Garmin sync failed, continuing with cached
data", "Calendar event was deleted on Google", "stored timezone is unknown on this
machine". Something outside the app did not work.

By that line, only about **13 sites** convert — nine in `garmin/` (eight in `sync.py`, one
in `pmc.py`), two in `calendar_reconcile.py`, one in `google_calendar.py`, one in
`clock.py` — plus the `Warning: Failed to log LLM exchange` in `openrouter.py`, which is
the log failing to log and belongs in the journal more than anywhere. Everything else keeps
its `print`.

`config.py`'s "Failed to load config.yaml" reads like a fourteenth and is not one. It fires
from `config = Config()` at module scope, before any run exists — and the journal needs
that same config to know which directory to write to. It stays a `print`.

`clock.py`'s is the one conversion with a trap in it, worth naming so nobody re-introduces
it. `_resolve_stored` warns when the stored zone is unknown, and it does so while
`active_zone()`'s cache is still unset, so anything it calls that asks for the zone again
recurses until the stack runs out. It does not here, because §4 takes the journal's day and
its timestamps from the system clock and `journal.py` imports nothing from
`trainmate.clock`. That is the third reason for the UTC file name.

`warn()` and `fail()` also fold the colouring in, so the twenty hand-written `Warning: `
prefixes stop being twenty independent decisions about capitalisation and colour.

### 5.4 The failures, for free

`main()` and the REPL loop already catch every exception a handler throws — that boundary
exists and is documented. The run bracket sits just inside it, so **every failed command
records its own traceback with no new handler anywhere.** Today that traceback is
discarded unless the athlete happened to pass `--debug`, which they never do, because the
run that mattered already finished.

That single hook is the largest thing this design buys and it costs one `try`/`except`.

### 5.5 The silent swallows

The ten `except Exception: pass` sites become `except Exception: journal.debug(...)`.
They stay silent on screen. They stop being silent on disk. A structural test pins this
(§11).

### 5.6 What the athlete answered

The verbs above record what the app did on its own. They do not record the one thing the
athlete contributes to a run: the answer to a yes/no question.

That gap is not cosmetic. A declined `workout generate` returns from its handler
normally — `confirm()` returns `False`, the handler prints "Workouts discarded" and
returns — so the run ends `ok`, with the same duration and the same token count as a run
that applied. Two rows in the listing, identical, one of which changed the plan and one of
which changed nothing:

```
81c0901f  W g -m 19   52,270 tok · 187.3s  ok
ba59ada8  W g -m 19   54,621 tok · 217.7s  ok
```

`cancelled` does not close this. It is `PromptCancelled`, which only the structured
front-end raises — a `/cancel`, an idle timeout, a dead answer channel. So the journal
already records *the athlete never answered* and has no way to record *the athlete said
no*, which is the far more common of the two.

Two of these answers also write to the database. Declining "a plan-shaping input has
changed, regenerate?" re-stamps the macrocycle's config hash, so the warning never fires
again; `workout generate` does the same when the athlete accepts the out-of-date plan.
A single keystroke permanently silences a future warning and leaves no trace anywhere.
That is exactly the "what it decided to skip" of §2.1.

**The record goes in the broker, not at the call sites.** All 29 questions in the tree —
28 `confirm`, one `choose` — go through `trainmate/prompt.py`, and nothing else asks. One
edit there covers every command on both transports, and a question added next year is
covered the day it lands. Twenty-nine `journal.note(...)` calls beside twenty-nine
prompts is the drift §5 opens by warning about.

It costs `prompt.py` its stdlib-only import, which is what kept it unit-testable with a
pair of `StringIO` streams. The import is deferred into `_record_answer` instead, so the
module still imports nothing at import time, and `journal.note` never raises (§4.3), so a
prompt cannot fail on its log.

**What it says.** One `note` at `info`, carrying the question and the answer:

```
+18.2s  info  note  Schedule these 7 workout(s) and push them to Google Calendar? → no
```

with `d: {"answer": false}`. The question is already prose written for a human, which is
the same reason §5.2 costs so little; it is flattened to one line and stripped of colour,
because it was written for a terminal and this is not one. `info`, not `warn` — declining
is not a problem, and `warn` would flip a healthy run's END column.

**Three things it keeps apart.**

*An answer is not a default.* `TtyPrompt` falls back to the default on EOF, which is cron
or a pipe — nobody answered. Those records carry `"defaulted": true` and the log does not
claim a decision that was never made. A structured front-end that replies without an
`answer` field is the same case.

*An answer is not a cancel.* A cancelled prompt records the question that was open, so a
`cancelled` run says what it was waiting on rather than only that it stopped.

*A yes/no is not free text.* `ask_text` is not journalled at all. Its answer is the
athlete's own words, which §4.4 keeps out. The one confirm whose question contains
athlete-derived text — "Add constraint: {title}?", extracted from the `-m` note — is
already covered, because that note is in `run.start` verbatim.

**Why `note` and not a tenth `ev`.** §4.2 says a name is earned by turning out to be worth
filtering on repeatedly, and `d.answer` is queryable today. The honest counter-argument is
that every other `note` is the app deciding for itself and this is the only record of the
athlete deciding, which is a real difference in kind. If `tm journal --declined` ever wants
to be a flag, that is the moment it earns `prompt.answer`.

**Which run it lands on.** `record()` reads the innermost open run of the process that
writes, so the note carries the run of the command that asked. On a TTY that is the
`run_once` bracket around the handler. Under the bot the CLI is a subprocess and
`JsonPrompt.confirm` blocks and returns *inside the child*, so the note lands on the
child's run — the same id as that command's other events, not the bot's long-lived one.
This matters because the bot already logs `answer: Yes` through `_log`, and that record is
written in the bot's process, on the bot's run. The two are not redundant: one says a
button was tapped, the other says which question got which answer, and only the second is
in the run you open when you ask what that command did.

**The outcome vocabulary does not grow.** A fourth outcome cannot work: a multi-goal `plan
generate` asks once per goal and one run may hold two yeses and a no. If the listing later
wants to mark declined runs, the way in is a `declines` counter on the `run.end` rollup
beside `warns` and `errors`, which keeps the §3 promise that the list view never scans a
run's events. Not built.

## 6. The LLM exchange log, joined in

The markdown files stay. They are the right format for what they hold: a document you
open and read when a prompt misbehaves, not a record you query. Nothing about their
content changes. Three things around them do.

**They gain the run id in the filename.**
`20260826_061502_184213_workout_adapt.md` becomes
`20260826_061502_184213_a3f91c2e_workout_adapt.md`, so
`ls logs/llm_exchanges/*a3f91c2e*` is the whole of "show me the prompts from that run".

The run id is the join, not the timestamp — deliberately. `_log_exchange` names its files
from `datetime.now()`, the machine's local clock, while the journal is UTC (§4). Matching
by time would mean reconciling two zones on every lookup; matching by id is a glob. (The
directory also holds two filename generations already: files before roughly 2026-08 have
no microsecond field. Nothing needs them to agree except §10, which only ever looks at the
leading `YYYYMMDD_HHMMSS`.)

**Each call emits an `llm.call` record** carrying model, label, prompt/completion/total
tokens, elapsed milliseconds, success or failure, and the path to the markdown file. Two
of the three asides in `openrouter.py` — the "Querying OpenRouter with model" line and the
token-usage line — become that one record, keeping their printed form on a terminal. This
is the join that does not exist today: from a run you reach its prompts, and from a prompt
you reach the command that asked for it.

**The exchange directory moves to where the config says.** `config.llm_logs_dir` currently
resolves against the *code* directory:

```python
return os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "llm_exchanges")
```

Every other path in `Config` — `db_path`, `science_dir`, `service_account_file` — resolves
against `CONFIG_DIR`, deliberately, so that a second athlete running the same checkout
under `TRAINMATE_CONFIG` keeps their state beside their own config. This one does not, so
the second instance writes its exchanges into the first athlete's log directory and the
two are interleaved with no way to tell them apart. It is a small bug and it is live —
the companion instance shipped in `318b528`. Both log directories move under a single
`logging.dir` key that resolves the same way as its neighbours.

For the primary instance this is a **no-op on disk**: its `CONFIG_DIR` *is* the repo root,
which is what the code-relative path already resolves to. The 218 existing files stay
exactly where they are and no migration is needed. Only a `TRAINMATE_CONFIG` instance
moves, which is the point.

## 7. `tm journal`

A log nobody can read is a directory that fills up. The command is what makes this a
feature rather than a side effect.

It is `journal` and not `log` for the reason §5.2 gives: `tm log` would read as "my
training log", which is what the workouts already are. It needs an entry in
`COMMAND_ORDER` in `trainmate_cli.py` like every other top-level command
(`DESIGN_cli_noargs.md` §c).

```
$ tm journal
RUN       WHEN                  SRC   COMMAND                          TIME   LLM       END
a3f91c2e  2026-08-26 Wed 06:15  push  bot morning                     24.1s   1 · 41k   ok
7d20b8a4  2026-08-25 Tue 21:03  bot   workout adapt -m "legs heavy…   31.7s   1 · 38k   ok
c8b17f30  2026-08-25 Tue 08:40  cli   data pull -d 7                   8.2s   —         warn
0b4a7712  2026-08-24 Mon 22:10  bot   plan generate                       —   1 · 96k   ?
5a0e1d99  2026-08-24 Mon 19:22  cli   plan generate -g 2              96.4s   2 · 210k  FAILED

warn    c8b17f30  Garmin sync failed, continuing with cached data
FAILED  5a0e1d99  KeyError: 'mesocycles'

END  ok = finished · warn = finished, but logged a warning or an error · FAILED = raised —
     'journal <id>' has the traceback · ? = no end recorded: still running, or killed
LLM  model calls · tokens
9 read-only run(s) hidden (-a for all) · command lines clipped (-v for the full text)
```

`END` is the outcome plus what the run wrote: `ok`, `warn` (finished, but logged a warning
or an error), `cancelled`, `FAILED`, and `?` for a run with no `run.end` — still running,
or killed before it could write one (§3). `0b4a7712` above is a `plan generate` the bot's
watchdog killed; without the `?` row it would simply not be in this listing.

```
$ tm journal 5a0e
run 5a0e1d99 · plan generate -g 2
  cli · pid 48213 · config.yaml · trainmate.db
  2026-08-24 Mon 19:22 → 19:23 · 96.4s · failed (exit 1)

   +0.0s  info   run.start
   +0.3s  info   note         Auto-syncing Garmin 2026-08-22..2026-08-24...
   +6.1s  info   garmin.pull  3 activities, 3 days of metrics
   +6.2s  info   note         Querying OpenRouter to generate macrocycle and mesocycles...
  +94.9s  info   llm.call     plan_generate · claude-opus-4 · 210,412 tok · 88.4s
                              logs/llm_exchanges/20260824_192213_512044_5a0e1d99_plan_generate.md
  +96.3s  error  run.end      failed · KeyError: 'mesocycles'
                              Traceback (most recent call last):
                                ...
```

An ambiguous id prefix lists the runs it matched rather than guessing, git-style.

```
$ tm journal --cost --since 2026-08-01
MODEL                       RUNS  CALLS      TOKENS
anthropic/claude-opus-4       18     22   1,940,220
google/gemini-3.5-flash       61     61     412,880
                              79     83   2,353,100

COMMAND                     RUNS  CALLS      TOKENS
plan generate                  6      9   1,102,400
workout generate               9     11     720,300
workout adapt                 31     31     490,100
```

The flags: `-n` for how many, `-d` for a window (the shared range grammar,
`DESIGN_cli_selectors.md` §3), `--source push` for the unattended runs, `--failed` for runs
that failed, were killed, or logged an error, `--command "workout adapt"` for one command's
history, `-a` for the read-only views the listing leaves out (§7.1), `-v` for command lines
and warnings in full rather than clipped (§7.2, §7.3), `--cost` for the rollup above, and
`--follow` to tail the file while the bot runs. `tm journal prune` is a real sub-command, not a positional, so
it cannot be confused with a run id.

Timestamps display in the athlete's timezone through the existing `fmt_timestamp`, which
renders `YYYY-MM-DD Ddd HH:MM` — no seconds anywhere, on purpose. Sub-minute resolution is
what the `+96.3s` offsets in the detail view are for, and it is relative time you want
there anyway. The listing goes through `render_table`, so it collapses to the vertical
record layout on a phone like every other table.

### 7.1 The listing is what the app *did*

`tm workout list` changes nothing. Neither does `plan show`, `status`, `journal` itself, or
any `-h`. Left in, they **are** the listing: on an ordinary day the athlete looks at things
far more often than the app writes anything, and the two runs worth reading — the adapt that
reshaped the week, the morning push that warned — sit under a screenful of views. The
`19ce4402  bot  status  0.6s` row this table used to carry is the shape of the problem: it
costs a line and answers no question anyone asks of a log.

So the default listing leaves the views out and `-a` brings them back. What counts as a view
is the **verb** — the last word of the command: `list`, `show` and every `show-*`, `status`,
`progress`, `compare`, `batches`, `versions`, `diff`, `journal`, `help`, `shell`, and a bare
`settings`, which is `settings list` (`DESIGN_cli_noargs.md` §a3). Keying on the verb means
one entry covers every group's `list`, and a `show` added under a new group tomorrow is
covered the day it lands. `TestReadOnlyVerbs` walks the real parser tree and fails the day
one of those names stops existing, because a stale entry hides nothing and says nothing.

Three things override it, because "it only looked" is a claim about a *quiet* run:

* **trouble** — failed, killed, or having logged a warning or an error. An exception raised
  inside `workout list` is exactly what this listing exists to put in front of you;
* **a model call.** The tokens were spent whoever asked;
* **`--command`.** Naming a command is asking for it, views included.

**Why not "did it write anything?"** That is the better rule and it is not available:
nothing emits `db.write` or `calendar.write` today — §4.2 reserves the events, no caller
uses them. The verb is the honest approximation until they exist; the day they do, this
becomes a filter on the record rather than on the name.

**What the record has to carry for this to work.** The athlete types prefixes — `wo li`,
`j` (§d of `DESIGN_cli_noargs.md`) — and `run.start` stores the argv verbatim, which is
right for a record and useless for a filter. So the parse now names the run:
`journal.name_run` puts the canonical `workout list` on `run.end`, and the listing filters,
groups (`--cost`) and matches (`--command "workout adapt"`) on that instead of on the typed
line. The screen still shows what was typed — that is what the athlete recognises.

A run killed before its parse never got a name, and a run recorded before this existed has
none either. Both fall back to the words they were typed as, and a word that matches nothing
is **listed** rather than hidden: never hide what you cannot classify. The unnameable runs
this rule used to be awkward about — a bare command group, a mistyped line, an `-h` — are
no longer written at all (§3), so what still reaches it is only what is genuinely
unclassifiable. The `-h` test on the typed argv stays regardless: the directory holds up to
`logging.retain_days` of records written while those runs were still journalled.

### 7.2 Nothing wraps, and the columns say what they mean

`workout adapt -m '<the athlete's whole note about the session>'` is a 380-character command
line. Printed in full it wrapped four times, and a table that wraps is no longer a table:
every column after it lands in a different place on every row.

COMMAND is the only cell with no upper bound, so it is the one that gives way. `render_table`
takes a `flex` column: it measures every other column, gives that one what is left of the
screen, and clips its cells with `truncate_visible`, which counts colour as no width and
ends the cell in `…`. `-v` prints the line in full instead and lets the terminal do what it
likes with it — the run id and `journal <id>` are the better way to read a long one anyway.

"The screen" is `util.display_width`: the real terminal's width when there is one, and
`default_wrap_width` (80, or whatever TRAINMATE_WRAP_WIDTH says) when there is not. Piped
output, the tests and the bot therefore stay deterministic, and a wide terminal is used
rather than wasted. This is the one table that asks for the window, because it is the one
whose content has no natural width — everywhere else 80 columns is a deliberate budget.

Under the table, in gray: what `END` says, glossed for the outcomes **actually on screen**
(a legend explaining `cancelled` when nothing was cancelled is a paragraph the eye learns to
skip), `LLM  model calls · tokens` when that column has anything in it, and then a dim line
naming what was left out — how many views, what was clipped to fit, how many older runs —
each with the flag that brings it back. A legend is one of the lines §3 of
`DESIGN_output_verbosity.md` keeps at answer level: you cannot read the column without it.

**Tokens, not money.** The rollup counts tokens because tokens are what the response
carries today. Read it as a volume, not a bill: prompt caching means the two are not
proportional, and verifying that caching works is what `openrouter.py`'s token aside was
added for in the first place. OpenRouter can also return the charged cost of a call when
the request asks for it; if that turns out to work as documented, `--cost` gains a currency
column and the record gains one field. It is a small follow-up, not a reason to hold the
design.

### 7.3 A row that is not `ok` says why

`warn` in the END column is a true statement and an unhelpful one. Three runs in a row
ended `warn` for the same reason — Garmin returned one activity with sparse HR zones and no
RPE, so its load is an underestimate — and nothing on screen said so. `util.warn` had
printed it at the time, hours earlier, inside a `bot morning` that scrolled past. Reading a
coded column and then running a second command to learn what it was coding is the failure
§7.2 fixed for the columns, in a different place: the listing knew and did not say.

So under the table, above the legend, one line per run that did not simply finish:

```
warn    5f922aa5  1 activity had low HR-zone coverage and no RPE; their load is an underes…
warn    1da7ed67  Garmin sync failed, continuing with cached data
FAILED  5a0e1d99  KeyError: 'mesocycles'
```

For a failed run that is the exception `run.end` already carries; for any other, the
**first** warning or error the run logged. First rather than last, and one rather than all:
the first thing to go wrong is usually why the rest did, and a listing that grows a
paragraph per row is a listing nobody reads again. The rest is one `journal <id>` away,
which is what the id on the line is for.

**Nothing new is written for this.** `_collect` already walks every record in the window to
find the two bracket records; it now also keeps the first `warn`/`error` message it passes,
attached after the pass rather than during it, because a run that spans midnight can log its
warning into a day file the bracket that owns it is not in. That costs no I/O, adds no field
to `run.end`, and — the reason to prefer it — works on every record already on disk,
including every one written before this existed.

The line is clipped to the screen like the command column, the message's own newlines
collapsed so that one warning is one row. `-v` prints it in full and there keeps the
newlines, because the multi-line warnings in `garmin/` are lists of activities and a list
flattened into a sentence is worse than no list. The dim footer names whichever of the two
was clipped, so `-v` is never something to guess at.

## 8. The bot

`trainmate_bot.py` already has a private logger: `_log(chat_id, direction, msg)`,
called from every send, tap and command start, `print()` to the process's stdout. Where
that stdout goes depends entirely on how `./tm-bot` was launched, which means in practice
it goes nowhere.

Those calls also become `bot.event` records — *also*, not instead: they keep printing, so
an operator watching `./tm-bot` in a terminal keeps the live view they have today. The
bot process opens one long-lived run at startup, so its own lifetime — a `/restart`, a
prompt that timed out, a Stop button raised over a coach call and the tap that took it, a
command killed by the watchdog, the morning push firing — is a readable timeline. Each
spawned CLI subprocess is a run of its own with the bot's run id as its parent, and
`tm journal <bot-run>` walks into the children.

That long-lived run is why §3 needs the `?` outcome and §10 needs its own trigger. It has
no `run.end` for as long as it is up, and none at all if the supervisor kills it — which
is the normal way it ends.

The morning push is the case this exists for. Today it is the least observable thing the
app does and the only thing that runs when nobody is looking.

## 9. Configuration

```yaml
logging:
  dir: logs                  # relative to the config file, like database: and science_dir:
  level: info                # debug | info | warn | error — what reaches the file
  retain_days: 90            # logs/runs/*.jsonl
  retain_exchange_days: 90   # logs/llm_exchanges/*.md
```

Every key has a working default, so an install with no `logging:` block behaves exactly
as described. `level: debug` is the switch that turns the swallowed-exception tier on; it
is off by default because those records are noise until the day they are not.

There is no CLI flag and no env var for verbosity. `TRAINMATE_VERBOSE` governs what
*prints*, which is a different question, and `-v` is spoken for on seven sub-commands
(`DESIGN_output_verbosity.md` §4).

## 10. Retention

The exchange directory has never been pruned. It is at 17 MB after three months —
about 5.6 MB a month, 218 files, 75 KB each — so it reaches roughly 70 MB a year and
keeps going. That is not urgent, but it is unbounded, and pruning it is three lines.

The journal itself is small: a busy day is a few dozen runs and a few hundred lines, so
call it 100 KB a month against the exchanges' 5.6 MB.

**The trigger is a stamp file, at most one sweep per UTC day.** `logs/runs/.pruned` holds
the date of the last sweep; a run whose `run.end` finds anything other than today's date
rewrites it and prunes. No cron, no daemon, no separate command in the normal path.
`tm journal prune` forces a sweep.

The obvious trigger — the run that had to *create* today's file — does not work here, and
it is worth saying why so it does not come back. The bot polls continuously, so the run
that writes the first line after midnight is almost always the bot's own long-lived run
(§8), whose `run.end` is days away and may never come at all. Hanging retention off the
creating run means retention never runs.

Pruning only ever deletes files inside the two log directories whose names begin with the
expected `YYYYMMDD_HHMMSS` timestamp, which is also what makes it safe across the exchange
directory's two filename generations (§6).

Ninety days is the default for both because the model-comparison work
(`docs/model_comparison_2026-08.md`) reads old exchanges, and a benchmark you cannot
re-read is a benchmark you have to re-run.

## 11. Testing

Six tests, matching the kinds `AGENTS.md` asks for.

**A unit test on the writer.** A record round-trips; a record never contains a newline; an
over-long traceback is elided in the middle and the whole record stays under 8 KB; a line
that is not valid JSON is skipped by the reader rather than raising; a write to an
unwritable directory neither raises nor prints more than once.

**A behavioural test on the bracket.** Run a handful of commands in-process against a
temporary journal directory: every `run.start` has a matching `run.end`; a command that
raises records the traceback and `outcome: failed`; a cancelled command records
`outcome: cancelled` and not `failed`; and three lines typed into `tm shell` produce four
runs — one for the shell and one per line, each naming the shell as its parent (§3). Then
the deferred half: a line that only printed usage or help writes nothing at all, a run is
on disk the moment it is named, and a drop that arrives after the run has spoken closes it
rather than abandoning it (§3).

**A behavioural test on the answers.** §5.6 turns on a record nothing else writes, so it
is pinned directly: a declined confirm lands on the run that asked it and the run still
ends `ok`; EOF is marked `defaulted` rather than read as a no; a cancelled prompt names
the question that was open; a confirm inside `tm shell` lands on the typed line's run and
not the shell's; and `ask_text` writes nothing at all, which is the §4.4 rule.

**A structural test on the read-only verbs.** §7.1 hides a run by matching the last word of
its command against a set of names, and that set lives in `cli/journal.py` while the names
live in the parser tree — two files, so the invariant needs a test that spans them.
`TestReadOnlyVerbs` walks the real tree and fails on any verb that no longer names a
command: a renamed or retired one would otherwise match nothing, silently, and its runs
would drift back into the listing with no symptom to notice.

**A structural test on where questions are asked.** The broker records every answer in one
place (§5.6), which only holds while it is the only thing that asks. So walk the AST of
`trainmate/cli/` and `trainmate/coach/` and fail on any call to `input`. Keyed on the
shape, so a handler written tomorrow is covered tomorrow. The REPL's line reader and the
Garmin MFA code sit outside both trees, and neither is a question about training.

**A structural test on the swallows.** `AGENTS.md` asks for a test that spans files when
the rule does, keyed on a shape rather than a list of names. The shape here is
`except Exception:` whose body is exactly `pass` — ten sites today — so walk the AST of
every file under `trainmate/` and the three entry points, and name the offender. A handler
that means to stay quiet says so with `journal.debug(...)`; one that says `pass` is an
accident, and the test cannot tell the difference from outside, which is the point.

Two shapes are deliberately outside it. A handler that names a narrow exception
(`ProcessLookupError`, `OSError`) is exempt: the type *is* the documentation of what was
expected. So is a broad handler whose body is a bare `return` or `continue`, because the
value it hands back is something the caller sees and can act on — `pass` is the only shape
with no effect outside itself, which is exactly why it is the one worth pinning.

## 12. Alternatives considered

**The `logging` module from the standard library.** A fair choice for the transport, and
it brings levels, rotation and `logging.exception()` for free. Rejected because what we
need on top of it is not small: a JSON formatter, a filter to inject the run id, and a
handler configured once per process across three entry points including an asyncio one.
That is more moving parts than the ~150-line module it would be wrapping, and stdlib
logging's global state is awkward in a REPL that reconfigures between commands — where the
run stack §3 describes is a list and two lines.

**Two SQLite tables.** §4.1.

**syslog / journald.** Ties a personal app to a system service, makes the log unreadable
without `journalctl`, and puts the athlete's free-text notes into the system journal.

**OpenTelemetry, or anything that ships somewhere.** One user, one laptop, no collector,
and §4.4 says the data does not leave the machine.

**Capturing stdout into the journal.** Tempting — the last few printed lines of a failed
run would be useful — but it means wrapping `sys.stdout` on every surface, it duplicates
the answer into a file that is not for answers, and `step`/`warn`/`fail` already capture
the lines that matter. Not done.

## 13. Phasing

The core is small and stands alone. The rest is optional and can be judged on whether the
core turns out to earn it.

**Phase 1 — the spine.** `trainmate/journal.py`, the run bracket in `run_once`, `step` /
`warn` / `fail` in `util.py`, the 29 aside reclassifications, the `llm.call` record, and
`tm journal` with its list and detail views. This is what answers all three questions in
§1. Roughly 150 lines of new code and a lot of one-word edits.

**Phase 2 — the corners.** The bot's eighteen `_log` calls, the `source` parameter on
`_cli_env` and the parent run id, the ten silent swallows, the run id in the exchange
filenames, the `logging.dir` fix from §6, retention, and `--cost`.

**Phase 3 — only if wanted.** `db.write` records tying a run to the rows it changed,
`--follow`, and error-only logging in the web app. A read-only dashboard GET neither
changes anything nor costs anything, so it is not a run; only its failures are, and they
are written with no run id at all — which also keeps the module-level run stack (§3) away
from Flask's threaded request handling.

## 14. What this does not change

No command gains or loses a capability. No screen changes: `step()` prints exactly where
`aside()` printed, and the 15 asides that stay are untouched. No date the athlete sees
moves: the UTC file name in §4 is an operator-facing detail, and every timestamp is
converted back through their zone before it is displayed. The database schema is
untouched, so there is no migration; the primary instance's 218 exchange files do not move
either (§6). The exchange markdown files keep their format and their contents. The bot's
behaviour is unchanged; it just stops throwing its own notes away.
