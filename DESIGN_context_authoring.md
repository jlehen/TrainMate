# Design: First-Party Daily-Context Authoring (`context` command)

**Status:** Implemented · **Date:** 2026-06-28 · **Companion to:** `DESIGN_calendar_context_ingest.md`

A top-level `context` command that lets TrainMate **author, list, and remove**
the same tagged daily-context events it already ingests. The ingest design
(`DESIGN_calendar_context_ingest.md`) only ever *read* tagged events; the sole
producer was an out-of-scope external syncer. This adds the **first-party
producer** for ad-hoc ambient signals (e.g. a heatwave) where standing up a
syncer is overkill.

---

## 1. Motivation

A severe heatwave tanks recovery the same way alcohol does — it's ambient
**daily context**, not a coarse `lifeevent`. The ingest channel already exists,
but the context tag lives in `extendedProperties.private` precisely so it is
**invisible and unsettable from the Google Calendar UI** (ingest §3). So a human
*cannot* hand-author a properly-tagged event; today only the external syncer
can. For one-off signals we want TrainMate itself to be that producer.

**Principle preserved:** TrainMate stays domain-agnostic. `metric` is opaque
free text (`"heat"`, `"sleep"`, …); no per-signal logic, no weather lookups.

---

## 2. Mental model — symmetric *home*, asymmetric *mechanism*

- **Inbound** is a *mirror*: `data pull` silently reconciles whatever tagged
  events exist on the calendar. Automatic, no intent.
- **Outbound** is *authoring*: it carries intent (which days, how severe) and
  can't be folded into an automatic pull — there's nothing to mirror until you
  say a thing happened.

The calendar stays the **single source of truth** in both directions. `context`
writes a tagged event to Google, then mirrors the row locally; the next
`data pull` re-confirms it idempotently by event id. Nothing in the ingest path
or schema changes.

---

## 3. Command surface

Top-level `context` (alias `c`), with subcommands:

| Subcommand | Alias | Purpose |
|---|---|---|
| `add`          | `a`  | Author a signal over a day or date range |
| `rm`           | `rm` | Remove signal(s) by id, or by range + metric |
| `list`         | `l`  | List signals, date-filtered and/or metric-filtered |
| `list-metrics` | `lm` | Show distinct metrics already in use |

Per the prompt-over-flags preference, `add` **prompts interactively** for any
field omitted on the command line.

### `context add` (`a`)
```
context add [--from YYYY-MM-DD] [--until YYYY-MM-DD] [-m METRIC] [--value N] [-l LABEL | TEXT]
```
- Date range defaults to **today** (single day) when neither bound is given;
  `--until` defaults to `--from`. One **all-day event per day** in the range —
  one event ⇒ one `daily_context` row, so it round-trips through the existing
  per-day ingest with zero schema change.
- `-m/--metric`: opaque category. Prompted if omitted; the prompt shows existing
  metrics (from `list-metrics`) to discourage `heat` vs `heatwave` drift.
- `--value`: optional **free numeric** the user supplies (severity, °C, count —
  TrainMate doesn't interpret it), kept for the future quantitative path
  (ingest §7). Free text alone is also fine.
- `TEXT` (positional) or `-l/--label`: the human/LLM blurb. `--label` takes
  precedence and avoids word-splitting for multi-word labels; the prompt ("Label")
  is optional. The event `summary` (and the mirrored `text`) is derived to match
  the ingested-event style: **with a label** → `label (value)` (e.g.
  `severe heatwave (38.0)`); **without** → `Metric: value` (e.g. `Alcohol: 2.0`).
  The value is dropped from the rendering when absent.
- **Idempotent upsert by (date, metric):** if a `trainmate-context` event with
  the same metric already exists on a day, **update** it (calendar + row) rather
  than create a duplicate. The schema deliberately doesn't enforce
  `UNIQUE(date,metric)` (ingest §5) — as the first-party producer we own this
  dedup, exactly as the syncer owns its own.
- After each write, **mirror locally** via `upsert_daily_context_by_event` using
  the returned event id, so signals show up before the next pull.

### `context rm` (`rm`)
```
context rm <id> [<id> ...]
context rm --from YYYY-MM-DD [--until ...] [-m METRIC]
```
- Deleting must remove the **calendar event** too, not just the local row:
  otherwise the event sits visible on the calendar and a token-reset recovery
  (`data wipe --calendar`) would resurrect it. So `rm` deletes the Google event
  then the row (mirrors the ingest's cancelled-event → delete path).
- Range/metric form deletes all matches; confirm `[y/N]` when >1 row matches.

### `context list` (`l`)
```
context list [--from ...] [--until ...] [-m METRIC]
```
- Defaults to the last `config.metrics_lookback_days` days when unbounded (15
  by default) — the **same window the coach reads context over** in the analysis
  path, so `list` shows what the coach sees, from one config knob. `--metric`
  filters. Prints `id · date · metric · value · text`.

### `context list-metrics` (`lm`)
- Distinct `metric`s in `daily_context` with **count** and **first/last date**,
  so you can see and reuse what you've already logged.

---

## 4. Code touch points

**`trainmate/cli/context.py`** (new, modeled on `cli/lifeevents.py`):
`run_context_add/_rm/_list/_list_metrics`. Subparsers + dispatch wired into
`trainmate_cli.py` next to the other top-level commands; reuse the existing
`--from/--until` date-arg idiom.

**`trainmate/google_calendar.py`** — two methods on `CalendarSyncer`:
- `add_context_event(date, metric, value, text, existing_event_id=None) -> str`
  — builds the body with `extendedProperties.private = {source:
  <calendar_context_tag>, metric, value?}`, all-day (`end = start + 1 day`);
  `update` when `existing_event_id` is set, else `insert`. Returns the id.
- reuse/generalize `delete_workout_event` → a plain `delete_event(event_id)`
  (it's already source-agnostic) for `rm`.

**`trainmate/db/dailycontext.py`** — small read/delete helpers:
- `get_daily_context(...)` gains an optional `metric` filter.
- `get_daily_context_by_id(id)` and `delete_daily_context(id)` for `rm`.
- `list_context_metrics() -> [{metric, count, first_date, last_date}]` for `lm`.

No table/column changes. No change to `sync_context`, the analysis prompt, or
the evidence fingerprint — authored events are indistinguishable from synced
ones downstream.

**Docs:** add an ARCHITECTURE.md note (per the keep-in-sync rule) and a line in
`DESIGN_calendar_context_ingest.md` §8 pointing here as the first-party producer.

---

## 5. Resolved decisions

1. **Group alias** → `c`.
2. **`value`** → free numeric the user supplies; TrainMate never interprets it.
3. **`list` default window** → `config.metrics_lookback_days` (15), matching the
   coach's context-read window.
4. **Multi-day `rm`** → range + metric form only; **no** logical-signal grouping.
   A multi-day signal is just N per-day rows; remove them by `--from/--until -m`.
