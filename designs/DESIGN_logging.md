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
called out to, what it decided to skip, how it ended. It is disposable after a few months
and nothing in the app ever reads it back.

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
finished, *including* a domain refusal ("no active plan") and including the exit-1 paths
where argparse printed help. `cancelled` is `PromptCancelled` — the front-end `/cancel`
or an idle timeout — which exits 130 today and is explicitly not a failure. `failed` is
an unhandled exception. Without this, `--failed` would list every `tm goal` typed without
a sub-command.

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
7d20b8a4  2026-08-25 Tue 21:03  bot   workout adapt -m "legs heavy"   31.7s   1 · 38k   ok
19ce4402  2026-08-25 Tue 21:01  bot   status                           0.6s   —         ok
c8b17f30  2026-08-25 Tue 08:40  cli   data pull -d 7                   8.2s   —         warn
0b4a7712  2026-08-24 Mon 22:10  bot   plan generate                       —   1 · 96k   ?
5a0e1d99  2026-08-24 Mon 19:22  cli   plan generate -g 2              96.4s   2 · 210k  FAILED
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

The flags: `-n` for how many, `--since`/`--until` for a window, `--source push` for the
unattended runs, `--failed` for runs that failed, were killed, or logged an error,
`--command "workout adapt"` for one command's history, `--cost` for the rollup above, and
`--follow` to tail the file while the bot runs. `tm journal prune` is a real sub-command,
not a positional, so it cannot be confused with a run id.

Timestamps display in the athlete's timezone through the existing `fmt_timestamp`, which
renders `YYYY-MM-DD Ddd HH:MM` — no seconds anywhere, on purpose. Sub-minute resolution is
what the `+96.3s` offsets in the detail view are for, and it is relative time you want
there anyway. The listing goes through `render_table`, so it collapses to the vertical
record layout on a phone like every other table.

**Tokens, not money.** The rollup counts tokens because tokens are what the response
carries today. Read it as a volume, not a bill: prompt caching means the two are not
proportional, and verifying that caching works is what `openrouter.py`'s token aside was
added for in the first place. OpenRouter can also return the charged cost of a call when
the request asks for it; if that turns out to work as documented, `--cost` gains a currency
column and the record gains one field. It is a small follow-up, not a reason to hold the
design.

## 8. The bot

`trainmate_bot.py` already has a private logger: `_log(chat_id, direction, msg)`,
eighteen call sites, `print()` to the process's stdout. Where that stdout goes depends
entirely on how `./tm-bot` was launched, which means in practice it goes nowhere.

Those eighteen calls also become `bot.event` records — *also*, not instead: they keep
printing, so an operator watching `./tm-bot` in a terminal keeps the live view they have
today. The bot process opens one long-lived run at startup, so its own lifetime — polling
paused and resumed, a `/restart`, a prompt that timed out, a command killed by the
watchdog, the morning push firing — is a readable timeline. Each spawned CLI subprocess is
a run of its own with the bot's run id as its parent, and `tm journal <bot-run>` walks into
the children.

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

Three tests, matching the kinds `AGENTS.md` asks for.

**A unit test on the writer.** A record round-trips; a record never contains a newline; an
over-long traceback is elided in the middle and the whole record stays under 8 KB; a line
that is not valid JSON is skipped by the reader rather than raising; a write to an
unwritable directory neither raises nor prints more than once.

**A behavioural test on the bracket.** Run a handful of commands in-process against a
temporary journal directory: every `run.start` has a matching `run.end`; a command that
raises records the traceback and `outcome: failed`; a cancelled command records
`outcome: cancelled` and not `failed`; and three lines typed into `tm shell` produce four
runs — one for the shell and one per line, each naming the shell as its parent (§3).

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
