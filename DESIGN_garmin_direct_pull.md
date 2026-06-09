# Design: Direct Garmin Pull with Watermark & Auto-Ensure

**Status:** Draft · **Date:** 2026-06-09 · **Branch:** `main`

This document captures the design for reworking `data pull` so TrainMate fetches
daily metrics and activities **directly from Garmin Connect**, tracks when data
was last pulled, and **auto-pulls when a read needs data it doesn't have or that
has gone stale**. It replaces the current Google Sheets ingestion path entirely.

Design discussion is complete; no code has been written yet. This doc is the
agreed spec and supersedes the README/ARCHITECTURE descriptions of "Garmin
Integration … via Google Sheets."

---

## 1. Motivation

Today data reaches the SQLite cache through **two hops**:

```
Garmin Connect ──[GarminScraper repo: sync.py + garmin_client.py]──► Google Sheet
               ──[TrainMate: google_sheets.py::sync_data]──► SQLite
```

`GarminScraper` (a separate repo, `/home/jlh/src/GarminScraper`) logs into Garmin
via `garminconnect`, computes TSS / RPE / HR-zones, and upserts rows into two
sheet tabs ("Daily Metrics", "Activities"). TrainMate's `data pull` then reads
the *entire* sheet (`A1:Z5000`) and both (a) ingests raw rows and (b) recomputes
derived metrics for every date.

This is brittle and manual: the user must run GarminScraper out-of-band to keep
the sheet current, and `data pull` only ever sees what the sheet happens to hold.

**The goal:** collapse the two hops into one. TrainMate talks to Garmin directly,
records a watermark of how far data has been pulled and when, and transparently
refreshes recent data (and bootstraps/backfills on request) as commands read it.

---

## 2. Goals / Non-Goals

**Goals**
- `data pull` fetches directly from Garmin; the Google Sheets metrics path is
  removed.
- The app knows when data was last pulled and **auto-refreshes forward**
  (recent/today) without the user asking.
- Reads that look further back than we hold are handled predictably: small gaps
  fill automatically, large ones are surfaced as a copy-pastable command.
- Manual `data pull` retains GarminScraper's full control (explicit date ranges,
  metrics-only / activities-only) plus a Garmin-call throttle.

**Non-Goals**
- The Flask web app does **not** pull from Garmin (it stays a pure DB reader —
  see §11).
- No automatic, unbounded backward backfill (it is slow and rate-limit-prone).
- No configurable athlete timezone override yet (machine-local only — §12).
- Google **Calendar** integration is untouched; it keeps the service account.

---

## 3. Architecture: one hop

```
Garmin Connect ──[TrainMate: trainmate/garmin.py]──► SQLite
                  (login + fetch + TSS/RPE/zone transforms,
                   then db.save_completed_activity / save_metric_cache / save_baseline)
```

The pure logic from GarminScraper — login/token handling, per-day metric
extraction, per-activity fetch, `calculate_tss`, HR-zone parsing — moves into a
new `trainmate/garmin.py`. The Sheets **write** side (`sheets_client.py`) and the
TrainMate Sheets **read** side (`google_sheets.py::GarminSheetsReader`) both go
away. The rows that GarminScraper used to shape for the sheet are instead shaped
for the existing `db.save_*` calls.

---

## 4. Module layout

**New: `trainmate/garmin.py`** — ported and adapted from GarminScraper:
- `GarminClient` — `login()` with token persistence + MFA callback (§11),
  `get_daily_metrics(date)`, `get_activities(start, end)`,
  `get_activity_hr_zones(id)`, `get_activity_rpe(id)`. Lifted essentially intact
  from `GarminScraper/src/garmin_client.py`.
- Pure transforms — `calculate_tss(activity, ftp, lthr)`, activity-row mapping,
  duration formatting. Lifted from `GarminScraper/src/sync.py`.
- A `pull(start_date, end_date, *, metrics=True, activities=True, throttle)`
  orchestration entry that fetches the range and writes via `db.save_*`. This is
  the single engine shared by manual `data pull` and the auto-ensure path.

**Removed:** `trainmate/google_sheets.py` (the `GarminSheetsReader` /
`sheets_reader` singleton). Its derived-metric logic (acute/chronic workload,
ACWR, 28-day baselines) is preserved but moves into the recompute pass (§10).

**Changed:** `trainmate_cli.py` `data pull` subcommand and the auto-ensure call
sites; `trainmate/db.py` (new `sync_state` table + helpers); `trainmate/config.py`
(new knobs); `trainmate/util.py` (local-date helper, §12).

**Dependency:** add `garminconnect` to TrainMate's requirements.

---

## 5. Data model

**Reused as-is:** `completed_activities` (PK `activity_id`), `athlete_metrics_cache`
(PK `date`), `athlete_baselines` (PK `date`). The shapes already match what the
Garmin transforms produce.

**New table — the watermark:**

```sql
CREATE TABLE IF NOT EXISTS sync_state (
    key            TEXT PRIMARY KEY,   -- e.g. 'garmin'
    through_date   TEXT,               -- forward high-water mark (YYYY-MM-DD, local)
    last_pull_utc  TEXT                -- ISO instant of last successful Garmin contact
);
```

- `through_date` is the **forward** high-water mark only. It advances after a
  successful pull (`max(through_date, end_of_pulled_range)`); a backward backfill
  never regresses it.
- `last_pull_utc` is an **instant** (UTC — see §12) compared against "now" for the
  freshness interval.
- `earliest` is not stored — it is `MIN(date)` over the cache.

**Null-row convention (load-bearing for gap detection).** On every pull, write an
`athlete_metrics_cache` row for **each day in the pulled range, even if all
metric fields are null.** This lets the table's date coverage distinguish:
- *"pulled, Garmin had nothing"* (row exists, fields null — e.g. no watch worn), from
- *"never pulled"* (no row).

Without this, legitimately empty days are indistinguishable from gaps and would
re-trigger a Garmin fetch on every read. With it, an **interior gap** is simply
"a date inside `[MIN(date), through_date]` that has no row."

---

## 6. Freshness model — two problems, one watermark

"Look back in time" hides two distinct needs:

- **(a) Backfill** — a read asks for dates we have never pulled (fresh DB, or a
  gap).
- **(b) Re-refresh** — days we *have* pulled go stale near the frontier: today's
  metrics finalize over the day (body battery, steps, sleep score), and RPE can
  be entered in Garmin hours after an activity.

The watermark `(through_date, last_pull_utc)` serves both. A read for display
window `[S, E]` needs Garmin contact when:

- `E > through_date` → **forward extension** (new days), or
- `E` is within the **mutable recent zone** and `now − last_pull_utc >
  garmin_refresh_minutes` → **re-refresh**, or
- the **required raw window** (next section) is not covered below `S` →
  **backfill**.

---

## 7. Auto-ensure

**Required raw window is wider than the display window.** To compute correct
ACWR / chronic load / baselines for date `S`, raw history is needed back to
`S − 28` (the derivation pad). So a read for `[S, E]` requires raw coverage of
`[S − 28, E]`.

**One entry point, called once per command.** A helper —
`ensure_data(start, end)` — is invoked at command entry with the window the
command is about to read. Deep code (coach.py, status, adapt) keeps reading the
DB exactly as today; it does not pull.

**Idempotent within a process.** `ensure_data` keeps an in-memory memo of what it
has already ensured this run, so repeated calls (coach.py reads metrics/activities
many times per command) cost nothing after the first. At most **one** real Garmin
round-trip per command invocation.

**Forward-refresh re-fetch span.** On a refresh, re-fetch the trailing
`garmin_mutable_days` (default 3) up to local today, not just today — to catch
late-finalizing sleep/HRV/RPE.

---

## 8. Pull policy

The unifying rule:

> **Forward-refresh is always automatic. Every other (backward / gap) fetch is
> automatic if small, a printed copy-pastable `data pull` command if large.**

| Situation | Behavior |
|---|---|
| Forward-refresh (keep recent/today current) | **Auto, silent**, throttled-but-cheap |
| Backward extension below `MIN(date)`, **small** (≤ `garmin_backfill_prompt_days`) | **Auto, silent** |
| Backward extension **large** (> threshold) | **Print command, don't run**; continue-with-warning |
| Interior gap, **small** | **Auto, silent** |
| Interior gap, **large** | **Print command, don't run**; continue-with-warning |
| **Cold start** (empty DB) — always the largest backward case | **Print command, don't run**; continue-with-warning |

**Continue-with-warning, never abort.** When a backfill is surfaced rather than
run, the command proceeds with whatever data exists and warns that
baselines/ACWR may be incomplete. The printed command is fully formed with
computed dates, e.g.:

```
This view needs data back to 2026-02-10, but the database starts at 2026-05-01.
Run:  python trainmate_cli.py data pull --start-date 2026-02-10 --end-date 2026-04-30
```

Cold start uses `garmin_initial_backfill_days` (default 90) to compute the
suggested range.

This policy also keeps the **automatic** path to small forward pulls only — which
is why MFA never blocks it (§11).

---

## 9. Manual `data pull`

Mirrors GarminScraper's interface so manual pulls are unbounded and explicit, and
skips the watermark/auto-ensure logic (it does exactly what is asked, then updates
the watermark on success):

- `--days N` — last N days (default 2, matching GarminScraper).
- `--start-date YYYY-MM-DD` / `--end-date YYYY-MM-DD` — explicit range (overrides
  `--days`; `--end-date` defaults to today).
- `--metrics-only` / `--activities-only` — mutually exclusive, bypass the other
  stream.
- `--sleep SECONDS` — throttle between Garmin calls (overrides
  `garmin_throttle_seconds`, default 0.2). Applied between days **and** between the
  multiplied per-activity calls.

After a successful manual pull, run the derived recompute (§10) and advance
`through_date` / `last_pull_utc`.

---

## 10. Derived recompute — full sweep

Adding/updating activities on date X changes acute workload for X..X+6, chronic /
ACWR for X..X+27, and baselines for X+1..X+28 — so recompute cannot be limited to
the days just ingested.

**Decision: ingest is incremental, derived recompute is a full sweep.** After any
pull, recompute `athlete_metrics_cache` (acute/chronic/ACWR) and
`athlete_baselines` for **all** dates from the activity/metric history. The
dataset is a few thousand local rows; the sweep is milliseconds and eliminates a
class of windowed-recompute bugs. This is the same math as today's
`sync_data`, relocated into a standalone `recompute_derived()`.

---

## 11. Authentication, MFA, and the web

**Token longevity makes MFA rare.** `garminconnect` persists a long-lived
(~1 year) OAuth1 token that silently refreshes the short-lived OAuth2 access
token. MFA is therefore only required on the **first** login (empty token store)
or a rare expiry/revocation. Every pull in between resumes from tokens
non-interactively.

**First login lands on a terminal command.** The cold-start backfill is a
**printed** `data pull` command the user runs in a terminal (§8) — exactly where
an MFA prompt is acceptable. The automatic forward-refresh path only runs once
tokens already exist.

**Non-interactive safety via TTY detection.** The `prompt_mfa` callback checks
`sys.stdin.isatty()`:
- TTY → prompt for the MFA code as today.
- Not a TTY (web, cron) → raise a custom `GarminAuthRequired` instead of blocking
  on `input()`.

No token-file pre-checking: we simply attempt `login(tokenstore=…)`; the
exception surfacing *is* the re-auth signal. Callers catch `GarminAuthRequired`,
fall back to cached DB data, and warn: *"Garmin re-auth required — run
`python trainmate_cli.py data pull` in a terminal."*

**Decision A — the web is a pure reader.** The Flask app never pulls from Garmin.
It renders whatever the DB holds and surfaces the watermark ("synced N ago") so
the UI can show freshness. All Garmin contact is owned by the CLI (optionally a
cron `data pull`). This avoids per-request latency, keeps auth out of the request
path entirely, and sidesteps token-store write races between server and CLI.

**Config & secrets.** Garmin credentials live in `config.yaml`
(`garmin_email` / `garmin_password`) and are **not** read from the environment —
`config.yaml` is gitignored, and keeping them out of env avoids leaking
credentials into process listings and shell history. FTP/LTHR are reused from the
existing `config.yaml` `user_profile` (so TSS is computed in TrainMate and the
GarminScraper `.env` duplication disappears). Token store defaults to
`~/.garminconnect`.

---

## 12. Timezone fix — calendar dates vs instants

Garmin keys all daily metrics and activities on the athlete's **local** calendar
date. TrainMate currently computes "today" in **UTC** (`datetime.now(timezone.utc)`),
which drifts a day from local at boundary hours — producing a lagging frontier
(Europe, after local midnight) or a permanent false "today missing" warning
(Americas, evenings).

Split the `datetime.now(timezone.utc)` uses:

- **Calendar dates → machine-local.** "What day is it" via
  `.strftime("%Y-%m-%d")` / `.date()`: sync frontier, mutable-zone, missing-today
  warning, default `adapt`/`status` date, year inference. Route **all** such sites
  through one shared helper (e.g. `util.today_str()` / `util.local_today()`) — not
  just the new frontier — so `adapt`/`status`/coach "today" stays consistent with
  the frontier.
  - Sites: `google_sheets.py:246` (moves with the rewrite), `db.py:384`,
    `cli.py:642/1110/1337/1362/1489/1592/1741/1742/1894`,
    `coach.py:132/1081/1288/1402/1739`.
- **Instants → stay UTC.** Moments compared against "now": `created_at`, freshness
  timestamps, `last_pull_utc`, LLM log times.
  - Keep-UTC: `db.py:46/768/777/791/831/915/1066`, `openrouter.py:51`.

No configurable timezone override for now; it can be added later inside the one
helper.

---

## 13. Configuration (`config.yaml`)

```yaml
garmin_refresh_minutes: 120        # min time between automatic Garmin hits
garmin_mutable_days: 3             # trailing days a forward-refresh re-fetches
garmin_backfill_prompt_days: 30    # small→auto / large→print-command cutoff (also gates interior gaps)
garmin_initial_backfill_days: 90   # range used to build the cold-start printed command
garmin_throttle_seconds: 0.2       # sleep between Garmin calls; --sleep overrides on `data pull`
```

Each gets a typed accessor on `Config` alongside the existing properties.

---

## 14. Migration & removal

- Delete `trainmate/google_sheets.py`; remove `google_sheet_id` usage for metrics
  (Calendar config stays).
- Existing accumulated history already lives in the DB from prior `data pull`
  runs, so there is no data migration — the watermark simply initializes from the
  current `MIN`/`MAX` cache dates on first run (or the DB is treated as cold start
  and the user is handed a backfill command).
- Update README ("via Google Sheets" → "directly from Garmin") and ARCHITECTURE.
- Tests: replace Sheets-reader tests with Garmin-transform + policy tests (mock
  the `garminconnect` client; assert watermark transitions, the §8 matrix, the
  null-row convention, and the full-sweep recompute).

---

## 15. Decisions & Open Questions

**Decided**
- Full removal of the Sheets metrics path (§3, §14).
- Backward/gap policy: small→auto, large→printed command; cold start = printed;
  continue-with-warning (§8).
- Derived recompute is a full sweep (§10).
- Web is a pure reader (Decision A, §11); off-TTY MFA raises `GarminAuthRequired`.
- All calendar-date sites switch to machine-local via one helper; instants stay
  UTC (§12).
- Config knobs and defaults (§13); creds via env; FTP/LTHR from `user_profile`.

**Open / to confirm during implementation**
- Exact helper name(s) in `util` (`today_str()` vs `local_today()`).
- Whether `ensure_data` lives in `trainmate/garmin.py` or a thin coordinator
  module called by the CLI.
- Whether a cron `data pull` should emit a desktop/push notification on
  `GarminAuthRequired` (currently: log + non-zero exit only).

---

## 16. Out of Scope

- Configurable athlete timezone / travel handling.
- Web-initiated pulls or in-request auth.
- Chasing interior gaps that are legitimately empty (no-watch) days — the null-row
  convention treats them as covered.
- Any change to Google Calendar sync.
- Backfilling RPE/zone detail for very old activities Garmin has since pruned.
