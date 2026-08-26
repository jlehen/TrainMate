# The athlete's timezone

## 1. What the timezone decides

Every date TrainMate computes is a *calendar* date in somebody's day: which workout is
due today, whether a session is behind or ahead, which day a Garmin metric belongs to,
where the planning window opens. Garmin itself keys daily metrics and activities on the
athlete's local calendar date, so a UTC frontier would drift a day at the boundary hours.

Before this, "somebody" was the machine: `util.today_date()` returned `date.today()`. That
is right on a laptop the athlete carries and wrong everywhere else — a server in UTC, a
home box left on a different setting, an athlete who moved. TrainMate is a
single-athlete-per-instance app, so the fix is one zone per instance, set by that athlete.

`trainmate/clock.py` is the one place that answers "what time is it", and
`util.today_date()` is its only caller for dates. Nothing else calls `date.today()`.

## 2. Which clocks follow it

Two clocks are the athlete's and now read `clock.now()`:

- **Every date computation**, through `util.today_date()` / `today_str()` — 240-odd call
  sites, all of which change together because they already funnelled through one helper.
- **The bot's morning push window** (`telegram.push.morning_time` / `morning_deadline`,
  DESIGN_bot_simple_frontend.md §4.3). Those are the hours the athlete wakes up in, not
  the hours the server is set to. The push loop drops the cached zone on every tick, since
  `timezone set` runs in a CLI subprocess and the bot would otherwise hold its first
  answer until a restart.

Two clocks are deliberately *not* the athlete's, because they are the machine's own
record of itself: the bot's console log lines and the LLM prompt-log filenames.

## 3. Where the zone lives, and what happens without one

The zone is an IANA name (`Europe/Paris`) in the `settings` table, under the `timezone`
key — the same generic key/value store the chosen LLM model uses
(DESIGN_model_selection.md §2). Three consequences:

- **Written from the CLI**, which is the whole point (§4). Config is hand-edited and the
  app never writes it, so a preference the athlete changes belongs in the database.
- **One source of truth.** There is no `timezone:` config key shadowing it. A second
  place to say the same thing is a second place to be wrong, and the config already
  carries what an instance *is* (which database, which Garmin account) rather than what
  its athlete currently prefers.
- **Survives a wipe**, like every other setting: a data wipe is about training history.

With no row stored, dates follow the machine — exactly the old behaviour, so an install
that never runs the command notices nothing. `describe()` reports that state as "the
machine's timezone (CEST, UTC+02:00)" rather than inventing an IANA name for it, because
the machine does not reliably know its own name, only its offset.

Two failure modes degrade instead of raising, because `today_date()` sits on every code
path and must never be the thing that takes a command down:

- A stored name this machine's tzdata does not carry (a hand-edited row — `timezone set`
  cannot write one) warns once and falls back to the machine.
- A database that momentarily cannot be read (a lock) falls back for that one call and is
  **not** cached, so the next call tries again.

The resolved zone is cached per process, since `today_date()` asks on every call and the
answer changes only when the athlete changes it. `set`/`reset` drop the cache, which is
what makes the change land in the same process (the REPL, the bot, the tests).

## 4. The CLI surface

`tm timezone` shows the active zone *with the local date and time it produces*, so the
athlete checks it against their watch rather than trusting a name they half-remember.
A bare `timezone` shows rather than printing help — the read-only-family exception
(DESIGN_cli_noargs.md §a3), the same one `model` takes.

`tm timezone set <zone>` stores it. Matching ignores case, and a name that matches nothing
lists the zones *containing* what was typed:

```
$ tm timezone set york
'york' is not a timezone name. Did you mean:
  America/New_York
```

That search is the discovery mechanism. Nobody knows the IANA name for where they live,
and a `timezone list` over 486 zones would be unreadable; typing the city is what an
athlete can actually do.

`tm timezone reset` forgets the zone and follows the machine again.

## 5. Stored instants are unchanged

Timestamp columns (`created_at`, `updated_at`, `last_pull_utc`, …) stay UTC. They are
instants, not calendar days, and UTC is the precise value — this is the storage rule in
AGENTS.md, and moving them would corrupt the comparison every one of them exists for.

What changed is the *display*: `util.fmt_timestamp()` converts to the athlete's zone on
the way to the screen, so "Planned: 2026-01-16 Fri 00:30" is the time they planned it,
not the time in Greenwich. A naive stored value is read as UTC, which is what every one
of those columns holds.

Dates in workout/goal rows are already plain calendar dates (`YYYY-MM-DD`) with no zone
attached, and Calendar events are all-day events keyed on them. Neither needs converting;
they just need to be *computed* in the right zone, which §1 is.
