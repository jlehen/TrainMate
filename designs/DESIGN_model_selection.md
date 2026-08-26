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
| `settings` command | Show the menu, change the choice (§4) |

The numbers the athlete types (`settings set coach-model 3`) are *display positions*, never
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
  # the default until `settings set coach-model` picks another.
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
    updated_at TEXT NOT NULL          -- UTC ISO, for "set 3d ago" in the listing
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
`trainmate.openrouter`. So `.model` becomes a lazily-resolved property:

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

Because the resolved value is cached, the registry's `on_change` hook for `coach-model`
calls `openrouter_client.reset_model()` to drop it — a set and a reset both change what the
next call should resolve to. One CLI invocation is one process, so this matters only in the
REPL (`tm shell`), where many commands share a process and the athlete reasonably expects a
model change to take effect on the very next line.

### §3.2 — The resolver module

New `trainmate/llm_models.py`, the single place that knows how config and DB combine:

| Function | Returns |
| --- | --- |
| `configured_models()` | The menu, in display order |
| `list_models()` | Display rows `{number, model, active}`, 1-based, config order |
| `active_model()` | The effective id per §3 (the `--llm-model` override is applied by the client) |
| `active_source()` | `"db"` or `"config"` — what the listing annotates |
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
  * moonshotai/kimi-k2   (active, not in config list — `settings set coach-model N` to move off)
```

Unnumbered because the numbers are config positions; there is no position to give it. This is a
display state, not an error — nothing fails, nothing is auto-corrected.

## §4 — Choosing the model

The `model` command this section described is gone: choosing the model is now one row of
the `settings` command, `settings set coach-model <n | id>`, and the numbered menu is
`settings list coach-model`. See DESIGN_settings.md §4 — a top-level command per
preference did not scale once the morning push and the router role wanted one too.

What §1–§3 above define is unchanged: the menu is `llm.models`, the choice is the
`settings.llm_model` row, the numbers are display positions and are never stored, and the
resolution order is override → stored → config default. The registry entry for
`coach-model` reuses `llm_models.resolve_token` as its validator, so an off-menu
identifier is still refused and `--llm-model` is still the escape hatch.

Three display rules survived the move into the generic listing, because each answers a
question the athlete would otherwise have to guess at:

- Where the active value came from is always shown — `config.yaml` when nothing is stored,
  `set 3d ago` when the athlete chose it. "Why this model?" is answerable from the listing.
- A stored model that has since left `llm.models` is listed unnumbered and flagged
  `not in config list` (§3.3). It is still what gets queried; nothing is auto-corrected.
- The two no-op paths say so rather than reporting a change that did not happen:
  `coach-model is X (unchanged).` and `Nothing stored for coach-model — already X.`

## §5 — Visibility elsewhere

`status` gains one dim line reporting the active model. Which model produced a plan matters when
comparing outputs across models, and the exchange logs already record it per call — but the log
is after the fact. One line in `status` answers it before the fact.

## §6 — Tests

`tests/test_cli_settings.py::TestCoachModel`: the identifier is stored rather than the
number, off-menu tokens are refused, `reset` returns to the config default, a stored model
dropped from `llm.models` stays active and is flagged, and `--llm-model` still wins over a
stored choice while writing nothing.

## §7 — Documentation

- `ARCHITECTURE.md`: the `settings` table, the `settings` command, `llm.models`.
- `README.md`: one line in the command list.
