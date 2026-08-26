# Settings: one command for every preference

## The problem

Two knobs had grown their own top-level command — `model` (DESIGN_model_selection.md) and
`timezone` (DESIGN_user_timezone.md) — and three more were about to want one: the morning
push time, its catch-up deadline, and the router model the simple chat front-end
classifies with. Those three lived only in `config.yaml`, which means they could not be
changed from Telegram at all; the companion athlete has no editor and no shell.

A top-level command per preference does not scale. Seven of them would crowd out the
commands that are about training, and each would carry its own listing, its own set/reset
pair and its own help text for what is, every time, the same three operations.

## The shape

| Part | Job |
| --- | --- |
| `config.yaml` | The **install default** — what a fresh instance ships with |
| DB → `settings` table | The **athlete's override** — what they changed, from anywhere |
| `trainmate/settings.py` | The **registry** — what a setting is, how it is validated, how the two combine |
| `settings` command | Show them, change one, forget one |

The registry is the single list. Adding a preference is one `Setting(...)` entry: it
appears in the listing, in `settings set`, in the structural tests, and in the athlete's
vocabulary without another line of command code.

## §1 — What is a setting, and what is not

A knob earns a row when the athlete might reasonably change it from a phone. That is the
whole test, and it is what keeps the listing at seven rows rather than fifty.

In: which model the coach reasons with, which model routes free text, the timezone, and
the four morning-push knobs (on/off, time, deadline, adapt-first).

Out, and staying in `config.yaml`: credentials (`llm.api_key`, `garmin.password`), file
paths (`database`, `science_dir`, `service_account_file`), the web bind address, the
Telegram plumbing (`bot_token`, `allowed_chat_ids`, timeouts, `wrap_width`), and every
coaching threshold calibrated against the science (`adherence_tolerance`, the PMC
constants, the staleness windows). None of those is a preference; several are dangerous
to change by accident, and the thresholds are calibrated as a set.

`telegram.ui` is deliberately not here either. `/ui` flips the persona in memory and a
restart returns to what `config.yaml` says — that ephemerality is the design
(DESIGN_bot_simple_frontend.md §5.6), and storing it would quietly reverse it.

## §2 — The registry

`trainmate/settings.py`. One `Setting` per knob:

```python
Setting(
    name=MORNING_TIME,                              # what the athlete types
    key="push_morning_time",                        # the settings-table row
    group="Morning push",                           # the listing heading
    summary="Local time the morning push fires",
    value_hint="HH:MM",                             # shown in the closing hint
    parse=parse_hhmm,                               # token -> stored form, or ValueError
    config_path=("telegram", "push", "morning_time"),
    fallback="08:00",                               # built-in default
)
```

Four fields carry the behaviour:

- **`parse`** maps a typed token to the stored form and raises `ValueError` with a
  ready-to-print message. It is the *only* validator: `settings set` calls it, and so does
  the config reader (§3), so a value can never enter the system unchecked. Existing
  validators are reused rather than re-written — `clock.resolve` for the zone (with its
  "did you mean Europe/Paris?" search), `llm_models.resolve_token` for both model roles.
- **`config_path`** is the nested key in `config.yaml`, or `config_default` when the
  default is not a plain key (the coaching model's default is the *first entry* of
  `llm.models`).
- **`fallback`** is the built-in default; `None` means "unset" is itself a meaningful
  state, and `unset_label` then says what unset means — `(this machine)` for the timezone,
  `(follows coach-model)` for the router.
- **`on_change`** drops whatever cache the new value invalidates: the resolved zone
  (`clock.reset_cache`), the model the OpenRouter client resolved on first use.

The storage key stays owned by the module that reads it — `clock.TIMEZONE_SETTING`,
`llm_models.LLM_MODEL_SETTING` — and the registry points at it, so neither the key nor
the resolution rule is written twice.

## §3 — Resolution

Highest wins:

1. `--llm-model <id>` — this invocation only, for the model roles (unchanged).
2. The `settings` row — what the athlete set.
3. `config.yaml` — what the install shipped with.
4. The built-in `fallback`.

`resolve(name)` returns the value *and* where it came from (`db` / `config` / `default`),
which is what the listing's third column prints. `value(name)` coerces: a bool for an
on/off knob, the string otherwise.

Config values go through the setting's own parser, which matters more than it sounds.
An unquoted `morning_time: 07:30` is the integer 450 to PyYAML, and a `router_model:`
naming a model that is not on `llm.models` is off the one allowlist (§4). Neither takes
the command down: the value is ignored, the built-in default applies, and the listing
names the problem under "Ignored in config.yaml". A key emptied out (`router_model: ""`)
says nothing at all, exactly like an absent one.

The `settings` table needed no migration — it already existed for `llm_model` and
`timezone`, and `db/wipes.py` already leaves it alone: a data wipe is about training
history, not about when the athlete wants to be woken.

## §4 — The command

```
settings                       # the table, same as `settings list`
settings list                  # the table
settings list <name>           # one setting in detail, with its menu or its clock
settings set <name> <value>    # validate, store, say what changed
settings reset <name>          # drop the row, fall back to config.yaml
```

`list`/`set`/`reset` rather than `show`/`set`/`reset`, so every sub-command keeps a
one-letter prefix. A bare `settings` lists rather than printing help — the read-only-family
exception (DESIGN_cli_noargs.md §a3) the `model` and `timezone` commands used to take.
Names take unambiguous prefixes the same way commands do: `settings set morning-d 12:00`
works, `settings set morning 12:00` lists the two it could mean.

```
=== SETTINGS ===

Coach
    coach-model       anthropic/claude-opus-5  set 3d ago
    router-model      (follows coach-model)    default

Clock
  * timezone          Europe/Paris             set today

Morning push
    push              on                       default
    morning-time      07:30                    config.yaml
    morning-deadline  15:00                    default
    adapt-first       off                      default
```

`settings list <name>` adds what the generic block cannot say: for `coach-model` the
numbered menu, marked with which entry coaches and which one routes; for `timezone` the
local date and time the zone produces, so it can be checked against a watch rather than
trusted by name. Those renderers live in `trainmate/cli/settings.py`, keyed by name, so
the registry stays free of display code.

### §4.1 — One allowlist for both model roles

`llm.models` is the menu, and both `coach-model` and `router-model` pick from it by number
or identifier. An off-menu identifier is refused for either role; `--llm-model` remains the
escape hatch for a one-off. The alternative — free-form identifiers for the router only —
would have bought one less config edit at the cost of the rule that makes the menu an
allowlist at all. Putting a cheap routing model on the menu is the price, and the
`coach-model` detail view marks which role each entry currently holds.

## §5 — The bot reads it live

`settings set` runs in a CLI subprocess; the bot is a long-lived process. Every knob it
reads is therefore re-read on each tick of the push loop (at most five minutes), next to
the `forget_timezone()` that was already there for the same reason. The push task is now
started whenever there is a chat to push to, with `push`, the persona and the window all
checked inside the loop — so `settings set push off` takes effect without a restart, the
way a `/ui` flip already did.

## §6 — What this replaced

`model` and `timezone` are gone as top-level commands; their behaviour is intact under
`settings`. `trainmate/cli/models.py` and `trainmate/cli/timezone.py` are deleted.
`llm_models.py` and `clock.py` keep the domain logic — the menu, the zone maths — and lose
their writers: one writer (`settings.write`) validates, stores and drops the cache for
every setting. `Config` loses the five accessors that only wrapped a key path
(`telegram_push_*`, `router_llm_model`) and gains `raw(*path)`, which the registry uses to
tell "absent" from "set to the default".
