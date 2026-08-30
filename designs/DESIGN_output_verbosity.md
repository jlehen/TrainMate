# Output verbosity: the answer, and everything said around it

## 1. The problem

TrainMate says a lot before it says anything. A `data show-activities` over Telegram
arrived like this:

```
Auto-syncing Garmin 2026-08-14..2026-08-16...
Logging into Garmin Connect (tokens: /home/jlh/.garminconnect)...
Found 3 activities in 2026-08-14..2026-08-16.
Fetching daily metrics for 3 day(s) 2026-08-14..2026-08-16...
Garmin sync completed.

=== COMPLETED ACTIVITIES (2026-08-10 to 2026-08-16) ===
...
```

Five lines of preamble before the table the athlete asked for. On the cached path it is a
different five words with the same shape:

```
Calendar signals is fresh (last sync 26m ago); using cache. Pass --force-pull to refresh now.
```

None of it is wrong. In a terminal it is genuinely useful: the command takes eight seconds
and those lines are how you know it is alive and what it is spending the time on.

But the Telegram front-end **buffers**. `trainmate_bot._drive` accumulates the subprocess's
stdout into a list and flushes it at a prompt boundary or at exit. So the athlete's phone
buzzes once, with the whole run in one `<pre>` block — the progress narration on top,
already finished, describing work that is over, pushing the answer below the fold. Live
narration delivered as history is not narration. It is padding.

The second half of the problem is the LLM's own prose. `workout adapt` runs daily and opens
with a "Decision Summary" the model writes free-hand. A representative one, in full:

> Readiness is currently acceptable rather than high-risk: 2026-08-16 RHR (50 bpm) and HRV
> (51 ms) are at/above baseline, sleep is fair (62), TSB remains positive (+6.4), ATL:CTL is
> normal (1.07), and the CTL ramp is conservative. Earlier depressed sleep/HRV episodes
> align chiefly with alcohol and lifestyle disruption and rebounded, rather than showing
> sustained training-driven autonomic fatigue. However, the first three days of this rebuild
> block accumulated 236 TSS, and today's endurance anchor exceeded planned load by 35% with
> 32 minutes above Z2 power, including threshold/VO2 work. Historical adherence also shows
> repeated excess duration/load on easy and strength sessions. Preserve Thursday's
> controlled climbing session and Sunday's protected FTP benchmark by modestly reducing
> Monday's muscular stress and making Tuesday's easy-power guard rail firmer; this corrects
> execution drift without reshaping the block or attempting catch-up changes.

150 words, wrapped at the phone's 48 columns: roughly 22 lines, every day, above a table
that already states which sessions changed and by how much. Most of it is the model
narrating the readings that did *not* change its mind, and re-listing numbers `status` and
the adaptation table print anyway.

## 2. Two audiences, one CLI

The bot is a shim over the real CLI (`trainmate_bot` runs `trainmate_cli.py` as a
subprocess), which is what keeps the two surfaces in permanent parity. That is worth
keeping. But parity of *commands* is not parity of *reading conditions*:

| | terminal | chat |
| --- | --- | --- |
| output arrives | line by line, as it happens | in one buffered block, at the end |
| progress narration | tells you the run is alive | describes work already finished |
| width | 80 columns | ~48 columns |
| cost of a spare line | a glance | a scroll |

So the split is not "verbose vs terse". It is **whether a line is still true when it
arrives**. A progress line is information about the present; delivered after the fact it is
only length. That is the whole rule, and it is why the gate keys off the front-end rather
than off a preference.

## 3. The three tiers

Every line a command prints is now one of three things.

**The answer.** What the command was asked for: the table, the listing, the proposal, the
outcome of a write. Always printed, on every front-end. `print`.

**A warning or an error.** Something that changes what the answer means, or says the
command could not do its job: Garmin re-auth needed, today's metrics missing, a Calendar
write that failed, a domain refusal. Always printed, on every front-end. `print`.

**An aside.** Progress narration, cache-reuse notes, defaulting notices, next-step hints,
standing caveats — side information. Printed on a terminal, suppressed in chat.
`trainmate.util.aside`, or `asides_enabled()` where the caller is building a list of lines
rather than printing them.

```python
def aside(text: str, color_fn=None) -> None:
    if not asides_enabled():
        return
    print((color_fn or dim)(text))
```

`asides_enabled()` reads `TRAINMATE_VERBOSE` first, then falls back to "not the JSON
front-end" via `trainmate.prompt.is_json_frontend` — the one place `TRAINMATE_FRONTEND` is
interpreted, so the transport decision is not re-derived here.

It is called `aside` and not `note` because this app already spends "note" four ways: plan
feedback notes, the athlete's `-m` note, daily-signal notes, and `PMC_TSB_LAG_NOTE`. One
of those is even a local variable (`trainmate/cli/plans.py`, `_feedback_rm`) that would
have shadowed the import.

### 3.1 When the narration *is* the answer

`garmin.pull` narrates its steps, and for every caller but one that narration is an aside —
it rides along inside `ensure_data` when some other command reads data. But `data pull` is
the command whose entire job is that sync. Suppress its narration and the athlete gets
`(no output)`.

So `pull` now **returns** a one-line summary — `Garmin 2026-08-14..2026-08-16: 3 activities,
3 days of metrics.` — and `run_data_pull` prints it. The steps stay asides; the result is an
answer. The distinction was never about the lines themselves, it was about who was asking.

### 3.2 Hints that survive

Two hints stayed at answer level because they are actionable and conditional rather than
standing boilerplate:

- the **block-boundary hint** (`DESIGN_block_boundary.md` §4): adapt cannot reach the next
  block, and the athlete has a few days to notice. It kept its place but lost half its
  bulk — four lines (headline, wrapped explanation, indented command, blank) became two.
- `ensure_recent_data`'s "today's Garmin metrics are not available yet", which changes how
  the answer should be read.

Everything of the form "you could now run X" — `model set`, `plan rollback`,
`workout rollback`, `benchmark record`, "if you applied the new plan, run workout
generate" — became an aside. So did the standing explanatory notes that print every single
time their table does and never change: `PMC_TSB_LAG_NOTE`, and the six measurement
caveats `intensity.format_notes` emits under every zone table.

The argument is not new to this doc — `cli/progress.py` had already reached it locally and
put `PMC_TSB_LAG_NOTE` behind `--explain`, with the comment *"a standing caveat, not news:
printing it on every invocation trained the eye to skip it."* That line stays as it is;
this generalises the rule it discovered.

One care point: `intensity.format_notes` feeds **both** the CLI tables and the LLM prompt,
where the caveats are load-bearing ("the app aligns; the LLM reasons"). So the gate goes on
the two CLI call sites — `cli/status.py` passes `notes=asides_enabled()` into the flag
`block_report` already had, `cli/progress.py` skips its own once-per-section block — and
never inside `intensity.py`.

### 3.3 What is deliberately still printed

Answer-level lines that *look* like chatter and are not: `(none)` and "(no results recorded
yet)" (they are the answer), "Plan-shaping: no — honored by daily 'workout adapt' only" (an
outcome), "Calendar sync skipped (--no-sync)" (why a thing did not happen), table legends
and provenance footers (you cannot read the numbers without them), and
`pmc_warming_note` (a transient caveat that changes what the numbers mean, and disappears
on its own once the PMC settles).

## 4. Why an env var and not a flag

`TRAINMATE_VERBOSE=1` forces asides on; `=0` forces them off. There is no CLI flag, and
that is not an oversight: `-v/--verbose` is already taken by seven sub-commands
(`status`, `constraint list`, `learnings list`, `data backfill-tss`, three under `workout`)
where it means "more detail in this listing" — a different axis entirely. A global
`--verbose` would land in the same namespace and quietly mean two things.

An env var also matches how the other front-end knobs already work
(`TRAINMATE_FRONTEND`, `TRAINMATE_WRAP_WIDTH`), and the bot needs no change: it sets
`TRAINMATE_FRONTEND=json`, which is already the signal.

## 5. The prose

The display side only fixes lines TrainMate writes. The Decision Summary is written by the
model, so it is fixed in the prompt, in two places.

**A shared style section.** `_build_system_prompt` now carries `## WRITING FOR THE ATHLETE`
between `## ACTIVE CONSTRAINTS` and `## TASK`, so every command built on it inherits it
(`plan generate`, `workout generate`, `workout adapt`, the editing paths). It says three
things: lead with the decision, name only the signals that drove it, and do not re-list
what the athlete's screen already shows. It also states that a length limit on a field is
a hard limit rather than a target.

Crucially it scopes itself: **it governs rationale and summary prose only.** A workout
`description` is the prescription the athlete trains from — intervals, zones, rest — and
shortening that would be a training regression, not a UX win. The section says so out loud
rather than trusting the model to infer it.

**Per-field limits**, in the `## RESPONSE FORMAT` schemas, on the three fields the athlete
actually reads:

| field | command | limit |
| --- | --- | --- |
| `reason` | `workout adapt` | at most 3 sentences (~60 words) |
| `change_reason` | `workout adapt` | one sentence, at most 20 words |
| `reasoning` | `workout generate` | at most 4 sentences |

### 5.1 What is deliberately left long

`plan generate`'s `strategy` is **not** capped, and this is the important exception. It is
not display prose: it is persisted on the macrocycle and re-injected into the system prompt
of every subsequent command as `## COACH LEARNINGS & ACTIVE PERIODIZATION STRATEGY`.
Shortening it would shrink the coach's standing context to buy a shorter one-off read.

Same reasoning spares `data analyze` / `data reflect`: their output becomes durable coach
learnings, they run rarely, and their prose is context rather than chatter.

The line to hold: **cap prose that is read once; leave prose that is read again by the
next prompt.** Before capping a new field, check whether anything feeds it back in —
`trainmate/coach/formatting.py` is where that happens. (`adaptation_summary` is safe:
only the per-workout `modification_reason` is re-injected, not the batch summary.)

## 6. What this does not change

No command gained or lost a capability, no data is stored differently, and the terminal
sees the same output it always did — every suppressed line is one `TRAINMATE_VERBOSE=1`
away. The bot is untouched: it already declared itself via `TRAINMATE_FRONTEND=json`, and
this change simply gives that declaration a second meaning.

## 7. The prompt context, and the wait

Rev 2. §3 sorted every line into answer / warning / aside, and that held — until you
measure the one thing it did not cover.

`plan generate` echoed the whole `PRIOR TRAINING REVIEW` block to the screen before
calling the model: the planned-vs-actual comparison of every plan the athlete has trained
through, with a per-sport zone table per plan. On this athlete's real database that is
**164 lines and about 1,900 words**, and re-laid-out at Telegram's 48 columns it becomes
**321 lines**. All of it above the strategy the athlete asked for.

### 7.1 Why it is not simply an aside

By §3's taxonomy it looks like an aside — side information, suppress it in chat, keep it
on the terminal. That is the wrong answer here, and the reason is the size. An aside is a
line you skim past; 164 of them is a scroll past. A terminal reader loses the answer off
the top of the screen just as surely as a phone reader loses it below the fold. So this
one is off on **both** front-ends by default, which no aside is.

It is also not free to produce. The block is built twice when shown — once at prompt
width for the model, once re-laid-out at the terminal's width, because the zone tables are
column-aligned and re-wrapping shreds the columns rather than fitting them
(`DESIGN_intensity_distribution.md` §6). Off the flag, that second full pass over the same
plans is skipped entirely.

What the **model** sees does not change. This is a screen decision, not a prompt one.

### 7.2 `--show-llm-context`, and why not `-v`

§4 turned down a global `--verbose` because `-v` already means "more detail in this
listing" on seven sub-commands. That argument has not weakened — `workout generate -v`
means "name each Calendar event as it is deleted and created", and there is no reading of
`-v` that covers both that and "echo the prompt context".

So the flag is `--show-llm-context`, on `plan generate`, next to the
`--show-llm-prompt-only` it is the sibling of: one prints the prompt and exits without
sending, the other prints the context and carries on. Deliberately **not** added to the
shared `llm_debug_parser`: `plan generate` is the only command that echoes prompt context,
and a flag accepted by five commands that ignore it is worse than a flag on one. Move it
there when a second command has something to show.

Left behind in its place is a one-line aside naming the flag, so the block is discoverable
rather than merely gone.

### 7.3 Flushing before the wait

Hiding the block fixes the terminal. Chat needs one more thing: `trainmate_bot._drive`
buffers stdout and flushes at a photo, a button row, a prompt, or exit — so with
`--show-llm-context` the 321 lines and the strategy arrive **in the same message**, after
a wait of tens of seconds, which is exactly the shape §1 set out to kill.

A fourth one-way sentinel, `\x1eTM-FLUSH` (`FLUSH_SENTINEL` / `emit_flush()` in
`trainmate/prompt.py`, `is_flush_request()` in `trainmate_bot.py`), ends the message where
it stands. Three details:

- **It gates itself.** `emit_flush()` is a no-op unless `is_json_frontend()`. A flush has
  no meaning where output already reaches the screen line by line, and putting the test
  inside means no call site has to remember it. `emit_photo` leaves that choice to its
  caller because a chart has a terminal story too; a flush does not.
- **`is_flush_request` returns a bool**, not the `Optional[dict]` its three siblings
  return. A flush carries no fields, and an always-empty dict reads as falsy at exactly
  the call site that must not treat it as absent.
- **It lives in `openrouter.complete()`**, immediately before the POST — not next to the
  print it was added for. That is the one point where every LLM command is about to go
  quiet, so all six get it, and the flush covers warnings printed before the call as well
  as the context block. An empty buffer flushes to nothing, so the extra markers cost
  nothing on a quiet run.

An old bot build against a new CLI is safe by construction: `_drive` already drops
unrecognised `\x1e` sentinels rather than forwarding them as chat text (§7.2 of
`DESIGN_progress_timeline.md`).

### 7.4 A wrap bug this surfaced

`wrap_text` has two branches: coloured paragraphs take a greedy word wrap that splits on
spaces only, uncoloured ones take `textwrap.wrap`, which breaks on hyphens. So a sentence
naming a flag rendered correctly on a colour terminal and came back as `--show-llm-` +
`context` on a piped run or over Telegram, where colour is stripped. `--force-pull` in
`ensure_data`'s cache note had the same latent break.

`break_on_hyphens=False` makes the two branches agree. Long words still break, so nothing
overflows the width.
