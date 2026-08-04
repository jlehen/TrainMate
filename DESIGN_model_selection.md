# Model selection

## The problem

Switching the LLM TrainMate talks to means editing `config.yaml` by hand. Today that file
carries the alternatives as commented-out lines:

```yaml
llm:
  #model: "openai/gpt-5.4"
  #model: "deepseek/deepseek-v4-pro"
  #model: "anthropic/claude-opus-4.8"
  model: "moonshotai/kimi-k3"
```

Switching = uncomment one, comment the previous one, save. That is fiddly, easy to get wrong
(two uncommented lines and YAML silently keeps the last), invisible from the CLI, and
impossible from Telegram — where there is no editor at all.

`--llm-model <id>` already exists as a per-invocation override, but it is a one-shot: nothing
persists, and it requires typing the full identifier every time.

## The shape

Three moving parts, each with one job:

| Part | Job |
| --- | --- |
| `config.yaml` → `llm.models` | The **menu**: models this install may use, in display order |
| DB → `settings` table | The **choice**: which menu entry is currently active |
| `model` command | Show the menu, change the choice |

The numbers the athlete types (`model set 3`) are *display positions in the menu*, never
stored. The database stores the model identifier string. Reorder the config list and an old
stored choice still points at the same model.

## §1 — Config: the menu

`llm.model` (a single string) is replaced by `llm.models` (a list). The commented-out
alternatives above become the list literally. The block below is an **example** menu, not the
shipped one — the list is meant to be edited freely, every install ends up with its own, and
`config_template.yaml` starts from a short two-entry list:

```yaml
llm:
  api_key: "sk-or-v1-…"
  # Models this install may use. `model list` numbers them in this order; the first entry is
  # the default until `model set` picks another.
  models:
    - model: "moonshotai/kimi-k3"
    - model: "openai/gpt-5.5"
    - model: "deepseek/deepseek-v4-pro"
    - model: "z-ai/glm-5.2"
    - model: "anthropic/claude-opus-4.8"
    - model: "google/gemini-3.5-flash"
```

An entry is either a `model:` mapping (as above) or a bare `- "openai/gpt-5.5"` string; both
parse to the same list. The mapping form leaves room for per-entry keys later without
reformatting the file, which is why the template uses it.

No labels or nicknames — the OpenRouter identifier is already short and is what appears in the
LLM exchange logs, so a second name for the same thing would only be one more thing to keep in
sync.

TrainMate is single-user (AGENTS.md), so this is a one-off config edit, not a migration: `model:`
goes away, `models:` arrives. `config_template.yaml` changes the same way. Nothing reads the old
key afterward.

New accessors on `Config` (`trainmate/config.py`), replacing `openrouter_model`:

- `llm_models -> list[str]` — the configured list. Empty/absent falls back to a single-entry
  list holding the current hard-coded default (`"google/gemini-3.5-flash"`), so a fresh install
  with a bare config still runs.
- `default_llm_model -> str` — `llm_models[0]`.

## §2 — Database: the choice

A new generic key/value table, so the next single-value preference doesn't need its own schema:

```sql
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL          -- UTC ISO, for "changed 3d ago" in `model` output
)
```

First and only key: `llm_model`. Created in `trainmate/db/base.py` alongside the other tables;
accessors `get_setting(key)` / `get_setting_row(key)` / `set_setting(key, value)` /
`clear_setting(key)` land in a new `trainmate/db/settings.py` mixin, following the `sync_state`
upsert pattern (`INSERT … ON CONFLICT(key) DO UPDATE`). `get_setting_row` returns the whole row
(`{key, value, updated_at}`) rather than just the value, because the "set 3d ago" annotation §4.1
asks for needs the timestamp — a value-only getter cannot answer *when*.

Not reused: `sync_state`. Its columns are dates and watermarks; a model identifier is neither.

`db/wipes.py` leaves `settings` alone — a data wipe is about training history, not about which
model the athlete prefers.

## §3 — Resolution order

Highest wins:

1. `--llm-model <id>` — this invocation only, never written to the DB (unchanged behavior).
2. `settings.llm_model` — the stored choice.
3. `config.llm_models[0]` — the config default.

### §3.1 — Where it resolves

`OpenRouterClient.__init__` currently does `self.model = config.openrouter_model` at *import*
time. It can't read the DB there without dragging a database connection into every import of
`trainmate.openrouter`. So `model` becomes a lazily-resolved property:

```python
@property
def model(self) -> str:
    if self._model is None:
        self._model = active_model()      # trainmate/llm_models.py, imports db lazily
    return self._model

@model.setter
def model(self, value: str) -> None:
    self._model = value
```

The setter keeps `openrouter_client.model = args.llm_model` in `trainmate_cli.py` working
verbatim, and keeps the existing test (`tests/test_cli_misc.py::test_llm_model_override`)
meaningful. Resolution happens on first use — after the DB exists, and after any
`--llm-model` override has been applied.

Because the resolved value is cached, both `model set` and `model reset` call
`openrouter_client.reset_model()` to drop it — either one changes what the next call should
resolve to. One CLI invocation is one process, so this matters only in the REPL (`tm shell`),
where many commands share a process and the athlete reasonably expects a `model set` to take
effect on the very next line.

### §3.2 — The resolver module

New `trainmate/llm_models.py`, the single place that knows how config and DB combine:

| Function | Returns |
| --- | --- |
| `configured_models()` | The menu, in display order |
| `list_models()` | Display rows `{number, model, active}`, 1-based, config order |
| `active_model()` | The effective id per §3 (the `--llm-model` override is applied by the client) |
| `active_source()` | `"db"` or `"config"` — what the `model` listing annotates |
| `stored_model()` / `stored_at()` | The raw stored choice and when it was written |
| `resolve_token(token)` | Number *or* id → id; raises `ValueError` with a printable message |
| `set_active_model(token)` | `resolve_token` then write; nothing is written when it raises |
| `clear_active_model()` | Deletes the row, falling back to the config default |

Imports `trainmate.db.db` inside the functions, not at module top, to keep
`trainmate.openrouter` importable without touching the database.

### §3.3 — A stored model that left the config list

The athlete edits `config.yaml` and drops the model that is currently stored. The stored id is
still a perfectly valid OpenRouter identifier, so TrainMate keeps using it rather than silently
switching models behind the athlete's back. `model list` shows it as an extra, unnumbered line:

```
  * moonshotai/kimi-k2   (active, not in config list — `model set N` to move off it)
```

Unnumbered because the numbers are config positions; there is no position to give it. This is a
display state, not an error — nothing fails, nothing is auto-corrected.

## §4 — The `model` command

A new top-level command, and the one command group that *acts* when run bare instead of printing
its help: a bare `model` prints the list. DESIGN_cli_noargs.md §a3 is where that exception and
the rule behind it live.

```
model                     # same as `model list`
model list                # numbered menu, active marked
model set <n | id>        # choose by number or by full identifier — persists
model use <n | id>        # registered alias of `set`
model reset               # forget the stored choice, fall back to the config default
```

`model l` and `model s` also work, but they are prefixes, not aliases — every command level gets
unambiguous prefixes for free and nothing registers them (DESIGN_cli_noargs.md §d, which is where
that distinction is defined). `use` is the one alias registered here, precisely because it is
*not* a prefix of `set`.

`model set` accepts either form because the number is only convenient when you have the list in
front of you; from a script or from memory the identifier is what you have. An identifier that
is *not* on the menu is refused: the menu is the allowlist, and `--llm-model` is already the
escape hatch for a one-off model you don't want to keep.

### §4.1 — Output

Against §1's example menu, and abridged — the real listing carries the usual `=== LLM MODELS ===`
header and a closing hint pointing at `model set`:

```
$ model
  1  moonshotai/kimi-k3
  2  openai/gpt-5.5
* 3  deepseek/deepseek-v4-pro     active (set 3d ago)
  4  z-ai/glm-5.2
  5  anthropic/claude-opus-4.8
  6  google/gemini-3.5-flash

$ model set 5
Model set to anthropic/claude-opus-4.8 (was deepseek/deepseek-v4-pro).

$ model set 9
No model numbered 9 — the list has 6 entries. Run `model` to see them.

$ model reset
Model reset to the config default: moonshotai/kimi-k3.
```

When no choice is stored, the active marker sits on entry 1 and the annotation reads
`active (config default)` instead of `set 3d ago`, so "why this model?" is answerable from the
listing alone.

When `--llm-model` is in play for the invocation, the listing adds a line noting the override is
active for this run only — otherwise `model` would report a model that isn't the one about to be
used.

The two no-op paths say so rather than reporting a change that did not happen: `model set` on the
already-active model prints `Model is X (unchanged).`, and `model reset` with nothing stored
prints `No stored choice — already on the config default: X.`

### §4.2 — Wiring

- `trainmate/cli/models.py`: `add_model_parser(subparsers)`, `run_model_list(args)`,
  `run_model_set(args)`, `run_model_reset(args)`.
- `trainmate_cli.py`: import the handlers, register the parser, add a `cmd == "model"` branch to
  the dispatcher, and add `"model"` to `COMMAND_ORDER[""]` — placed after `data`, before
  `shell`/`help` (it is configuration, not a daily-use command).
- `trainmate_bot.py`: nothing. The bot proxies arbitrary CLI command lines, so `model` and
  `model set 3` work over Telegram the moment the CLI has them. Optionally add `model` to
  `MENU_COMMANDS` for the Telegram command menu — cosmetic only.
- `trainmate_web.py`: out of scope.

## §5 — Visibility elsewhere

`status` gains one dim line reporting the active model. Which model produced a plan matters when
comparing outputs across models, and the exchange logs already record it per call — but the log
is after the fact. One line in `status` answers it before the fact.

## §6 — Tests

Extend `tests/test_cli_misc.py`:

- `model` with nothing stored marks entry 1 and says `config default`.
- `model set 3` writes the identifier (not the number) and the next `model` marks entry 3.
- `model set <full-id>` resolves the same way.
- `model set 0` / `model set 99` / `model set garbage` exit non-zero and change nothing.
- `model reset` clears the row; the config default is active again.
- Stored id absent from `llm.models` still resolves as active and is flagged in the listing.
- `--llm-model` still wins over a stored choice and leaves the DB untouched (extends the
  existing override test).

## §7 — Documentation

- `ARCHITECTURE.md`: §5 (new `settings` table), §7 (the `model` command), §9 (`llm.models`
  replaces `llm.model`), §6 if the singleton list mentions `openrouter_client.model`.
- `README.md`: one line in the command list.
