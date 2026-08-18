# Design: Direct Garmin Pull with Watermark & Auto-Ensure

**Status:** Implemented · **Date:** 2026-06-09 · **Branch:** `main`

This document captures the design for reworking `data pull` so TrainMate fetches
daily metrics and activities **directly from Garmin Connect**, tracks when data
was last pulled, and **auto-pulls when a read needs data it doesn't have or that
has gone stale**. It replaces the current Google Sheets ingestion path entirely.

> **Rev. 2 (2026-08-04) — post-implementation reconciliation.** Rev. 1 was the
> pre-implementation spec ("no code has been written yet"). The design shipped and its
> architecture held: one-hop pull, the `sync_state` watermark, `ensure_data` with the
> small-auto / large-print policy, continue-with-warning, the null-row convention, the
> full-sweep recompute, TTY-gated MFA, and the web as a pure reader are all live and
> unchanged in shape. Four things moved underneath it, and this revision folds them in
> rather than leaving the reader to discover them:
>
> 1. **`trainmate/garmin.py` became the package `trainmate/garmin/`** (§3, §4, §12).
> 2. **ACWR was retired** in favour of the PMC (CTL/ATL/TSB) and the ATL:CTL load ratio —
>    DESIGN_pmc_fitness_fatigue.md, DESIGN_load_ratio.md. Every acute/chronic/ACWR mention
>    below is marked superseded in place (§4, §7, §8, §10).
> 3. **The derivation pad grew from a fixed 28 days to `max(28, ceil(1.5·τ_CTL))` = 63**
>    at defaults, and gained an auto-pull exception so widening it doesn't nag (§7, §8).
> 4. **Config keys are nested under a `garmin:` section**, not the flat `garmin_*`
>    top-level keys rev. 1 wrote (§11, §13). Rev. 1's YAML would be silently ignored.

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
  *(Rev. 2: still true of the workout-sync side, but no longer of the pull path.
  `pull()` and `ensure_data()` now both call `_sync_calendar_context()` so daily-context
  events ride along with a Garmin read, and `data pull` stamps adherence onto past
  Calendar events — a later, separately-designed feature. See
  DESIGN_calendar_context_ingest.md and §9.)*

---

## 3. Architecture: one hop

```
Garmin Connect ──[TrainMate: trainmate/garmin/]──► SQLite
                  (login + fetch + measured-TSS / zone parsing / RPE read,
                   then db.save_completed_activity / save_metric_cache / save_baseline)
```

The pure logic from GarminScraper — login/token handling, per-day metric
extraction, per-activity fetch, TSS computation, HR-zone parsing — moves into a
new `trainmate/garmin` (rev. 1 shipped it as a single `garmin.py`; it is now a package —
§4). (The TSS/load math was subsequently reworked — see the
Load model note in §4 and ARCHITECTURE.md §12.) The Sheets **write** side
(`sheets_client.py`) and the
TrainMate Sheets **read** side (`google_sheets.py::GarminSheetsReader`) both go
away. The rows that GarminScraper used to shape for the sheet are instead shaped
for the existing `db.save_*` calls.

---

## 4. Module layout

**New: `trainmate/garmin/`** — ported and adapted from GarminScraper. Rev. 1 specified one
file; it grew past comfortable reading and was split into four submodules, all re-exported
from `trainmate/garmin/__init__.py` so `from trainmate import garmin`,
`garmin.pull(...)` and `patch.object(garmin, …)` all keep working unchanged:

| Submodule | Holds |
|---|---|
| `client.py` | `GarminClient`, `GarminAuthRequired`, `_derivation_pad_days()`, date helpers |
| `load.py` | the TSS / training-load model |
| `pmc.py` | `recompute_derived()`, `backfill_tss()`, the PMC EWMAs and baselines |
| `sync.py` | `pull()`, `ensure_data()`, the process memo, the printed-command policy |

- `GarminClient` — `login()` with token persistence + MFA callback (§11),
  `get_daily_metrics(date)`, `get_activities(start, end)`,
  `get_activity_hr_zones(id)`, `get_activity_power_zones(id)`, `get_activity_rpe(id)`.
  Lifted essentially intact from `GarminScraper/src/garmin_client.py`; the power-zone
  fetch and the shared `_parse_zone_entries` helper came later with the power-TSS branch
  of the load model below. Power zones return all-**None** (not zeros) when the activity
  had no power meter, so the columns stay NULL rather than reading as a real zero.
- Pure transforms — activity-row mapping, duration formatting, and the load
  model. **Load model (reworked after the initial port):** the `tss` column
  stores the *measured* TSS only — power TSS (Coggan 7-zone) if a power meter
  recorded, else hrTSS (Friel 5-zone), else NULL (`measured_tss`). Training
  **load** is derived on the fly (`activity_load`) via a best-available fallback:
  measured TSS when from power or HR with coverage ≥ 0.5, else the user-entered
  RPE as sRPE (`RPE × 10 × hours`). RPE is **never synthesised**. When the measurement
  *is* trustworthy but the athlete's RPE implies materially more strain
  (`sRPE / tss ≥ rpe_divergence_ratio`), `activity_load` **takes the load from RPE
  instead** — it does not merely annotate. `rpe_divergence` exposes the same ratio so the
  bump can be explained, and `load_method` names the provenance (`rpe_divergence`) so no
  surface prints a tag the number beside it didn't come from. Full details:
  ARCHITECTURE.md §12.
- A `pull(start_date, end_date, *, metrics=True, activities=True, throttle,
  advance_watermark=True)` orchestration entry that fetches the range and writes via
  `db.save_*`. This is the single engine shared by manual `data pull` and the auto-ensure
  path. `advance_watermark=False` lets a caller ingest a range without moving
  `through_date`.
- `backfill_tss(start_date=None, end_date=None, verbose=False) -> int` — recomputes the
  measured `tss` for stored activities from their saved zone seconds (no Garmin calls) and
  returns the number of rows that changed; CLI: `data backfill-tss [-d RANGE] [-v]`.

**Removed:** `trainmate/google_sheets.py` (the `GarminSheetsReader` /
`sheets_reader` singleton). Its derived-metric logic (acute/chronic workload,
ACWR, 28-day baselines) is preserved but moves into the recompute pass (§10).

> **Superseded (2026-07-31, DESIGN_load_ratio.md).** Acute/chronic workload and ACWR were
> retired: the columns are actively DROPped from `athlete_metrics_cache` on schema init
> and no `acwr` symbol survives in `trainmate/`. What the recompute pass inherited from
> the Sheets reader is the **28-day baselines**; the workload half was replaced by the
> PMC — CTL / ATL / TSB EWMAs (DESIGN_pmc_fitness_fatigue.md) plus the ATL:CTL **load
> ratio** that took over ACWR's job without fighting block periodization. Read every
> "acute/chronic/ACWR" below as "CTL/ATL/TSB + load ratio".

**Changed:** `trainmate_cli.py` `data pull` subcommand and the auto-ensure call
sites; `trainmate/db.py` (new `sync_state` table + helpers — now the `trainmate/db/`
package, `db/base.py` + `db/activities.py`); `trainmate/config.py`
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
    last_pull_utc  TEXT,               -- ISO instant of last successful Garmin contact
    sync_token     TEXT                -- rev. 2: opaque Calendar nextSyncToken
);
```

*Rev. 2 — the table went multi-tenant.* It was designed for one row but the `key` column
did its job: four sources now share it — `garmin` (this design), `calendar_context`
(DESIGN_calendar_context_ingest.md, the only user of the added `sync_token` column),
`reflect` and `bootstrap` (the coach's analysis watermarks). Each source owns a key and
leaves the columns it doesn't use NULL. Accessors are
`db.get_sync_state(key="garmin")` / `db.set_sync_state(through_date, last_pull_utc,
key="garmin", sync_token=None)`.

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
re-trigger a Garmin fetch on every read. With it, a **gap** is simply "a date in the
required raw window that has no metrics-cache row" — see §6 for why `through_date`
turned out not to be part of that test.

*Rev. 2 caveat — `--activities-only`.* The null row is written by the metrics ingest, so
`pull()` only writes it when `metrics=True`. A `data pull --activities-only` range still
reads as "never pulled" to `ensure_data`, which will fetch it again later. That is the
honest answer (metrics genuinely weren't fetched), but it means the convention holds for
the metrics path, not for every pull.

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
  data_refresh_minutes` (or `--force-pull`) → **re-refresh**, or
- the **required raw window** (next section) is not covered below `S` →
  **backfill**.

> **Rev. 2 — only the second test survived; coverage is decided by row presence.**
> `ensure_data` never reads `through_date`. It asks `db.get_metric_dates()` which days in
> the required raw window have a row, and adds the mutable zone when
> `now − last_pull_utc` is stale. Row presence **subsumes** the `E > through_date` test
> and is strictly more precise: a forward extension is just "the new days have no rows",
> and the same one test also catches interior gaps, backward gaps and pad warm-up, which
> a high-water mark cannot see. `through_date` is still written on every pull and still
> never regresses — it survives as the **display/reporting watermark** the dashboard
> renders as "synced through …", not as a coverage decision input. `last_pull_utc` is
> load-bearing, as designed.

---

## 7. Auto-ensure

**Required raw window is wider than the display window.** To compute correct
ACWR / chronic load / baselines for date `S`, raw history is needed back to
`S − 28` (the derivation pad). So a read for `[S, E]` requires raw coverage of
`[S − 28, E]`.

> **Rev. 2 — the pad is 63 days at defaults, and 28 is only its floor.** With ACWR gone
> (§4) the pad no longer sizes a 28-day chronic window; it has to warm the **CTL EWMA**,
> which needs far longer than a rolling average of the same nominal length.
> `_derivation_pad_days()` = `max(28, ceil(1.5 · pmc_ctl_days))` = **63** at the default
> τ_CTL = 42. The 28 floor pins the pad to the still-hardcoded 28-day baseline lookback in
> `recompute_derived()`; the 1.5·τ term warms CTL to ~78% at the left edge, and
> DESIGN_pmc_fitness_fatigue.md §3.4 carries the residual as an accuracy caveat rather
> than padding further. Read `S − 28` below as `S − _derivation_pad_days()`. The pad is
> read live from config, so changing `pmc_ctl_days` widens it — which is exactly the case
> §8's pad-warm-up exception exists to keep quiet.

**One entry point, called once per command.** A helper —
`ensure_data(start_date, end_date, force=False)` — is invoked at command entry with the
window the command is about to read. Deep code (coach.py, status, adapt) keeps reading the
DB exactly as today; it does not pull. `force` is the global `--force-pull` flag (§9).

**Idempotent within a process.** `ensure_data` keeps an in-memory memo of what it
has already ensured this run, so repeated calls (coach.py reads metrics/activities
many times per command) cost nothing after the first. At most **one** real Garmin
round-trip per command invocation.

> **Rev. 2 — the invariant is one `ensure_data` *pass*, not one round-trip.** The memo
> holds, and `--force-pull` deliberately bypasses it. But when the missing days form
> several non-contiguous regions, `ensure_data` calls `pull()` once **per contiguous
> region**, each with its own `client.login()`. Two regions — a pad warm-up far back and
> the recent mutable zone — is the normal case, not an edge case, and shows up as two
> "Auto-syncing Garmin …" lines. Merging them into one span would re-fetch every day in
> between, which is the more expensive mistake.

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
| *(rev. 2)* Gap **entirely before** the window — derivation-pad warm-up | **Auto**, up to the pad (63) |

**Rev. 2 — the pad-warm-up exception.** A missing region that ends before `S` was never
asked for by the user: it exists only to warm the derivation pad (§7), and it is bounded
by the pad, so its size is ours to choose rather than the user's to be nagged about. Those
regions get `limit = max(prompt_days, pad_days)` and therefore auto-pull up to 63 days,
above the 30-day prompt cutoff. Without it, the day the pad widened from 28 to 63 every
pre-existing install would have started printing "run `data pull -d …`" on *every*
command instead of quietly healing itself. Regions that overlap the requested window keep
the plain `prompt_days` limit. Covered by
`tests/test_garmin.py::…test_pad_only_gap_auto_pulls`.

**Continue-with-warning, never abort.** When a backfill is surfaced rather than
run, the command proceeds with whatever data exists and warns that
baselines/ACWR may be incomplete. *(Rev. 2: the warning now names baselines and the
CTL/ATL/TSB warm-up — see §4's superseded note.)* The printed command is fully formed with
computed dates, e.g.:

```
This view needs data back to 2026-02-10, but the database starts at 2026-05-01.
Run:  python trainmate_cli.py data pull -d 2026-02-10..2026-04-30
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

- `-d RANGE` — the shared selector (DESIGN_cli_selectors.md), read backward: `-d 7d` is
  the last 7 days, `-d A..B` an explicit range, `-d ..B` the 2 days ending B. Defaults to
  `2d`, matching GarminScraper.
- `--metrics-only` / `--activities-only` — mutually exclusive, bypass the other
  stream.
- `--sleep SECONDS` — throttle between Garmin calls (overrides
  `garmin_throttle_seconds`, default 0.2). Applied once **per day** on the metrics stream
  and once **per activity** on the activity stream. *(Rev. 2: rev. 1 said "between the
  multiplied per-activity calls" — the sleep actually lands after an activity's whole
  HR-zone + power-zone + RPE sub-fetch, not between those sub-calls. Per-activity
  granularity is the real, and adequate, throttle.)*
- *(Rev. 2)* `--no-mark` — skip the Calendar adherence ride-along below.

After a successful manual pull, run the derived recompute (§10) and advance
`through_date` / `last_pull_utc`.

**Rev. 2 — two additions to the flag set.**

*Calendar ride-along.* With fresh activity data in hand, `data pull` stamps the adherence
verdict onto past Calendar events over the pulled range, and `pull()` force-syncs daily
calendar context. Both are best-effort: a Calendar failure never breaks the pull, and it
all no-ops when no calendar is configured. `--no-mark` opts out of the adherence stamp.
This is a later feature layered onto the pull — DESIGN_calendar_context_ingest.md — so
read the flag list here as the Garmin surface, not the whole command.

*Global throttle-bypass pair.* `--force-pull` and `--no-pull` are mutually exclusive
global flags on every auto-ensuring command (not on `data pull`, which is already
explicit). `--force-pull` sets `ensure_data(force=True)`: it treats the mutable zone as
stale and bypasses the process memo, refreshing inside the `refresh_minutes` window.
`--no-pull` skips the auto-ensure entirely and reads purely from the SQLite cache. §6
mentions `--force-pull` in passing; this is where both live.

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

> **Superseded (rev. 2) — what the sweep computes.** The decision holds exactly; only the
> quantities changed. `recompute_derived()` now writes **`ctl` / `atl` / `tsb`** (EWMAs
> walked over every calendar day so rest days decay them) plus the 28-day
> `athlete_baselines`; `acute_workload` / `chronic_workload` / `acwr` are gone from the
> schema. The full-sweep rationale is if anything stronger under the PMC: an EWMA has
> unbounded memory, so an activity added on date X changes every derived value *after* X,
> not merely X..X+27. See DESIGN_pmc_fitness_fatigue.md.

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

> **Rev. 2 — Decision A is now enforced, not merely observed.** It used to hold because
> no route happened to call `pull()`, backed by a single `POST /api/metrics/pull` that
> answered 409 with the CLI command. The dashboard has since been demoted to read-only
> repo-wide: a `before_request` guard in `trainmate_web.py` **405s every mutating verb**
> (POST/PUT/PATCH/DELETE) in one place, and the pull endpoint is gone. A route that wants
> to write has to delete the guard first — which is the point. The dashboard still reads
> `sync_state` and renders the freshness line, exactly as designed here.

**Config & secrets.** Garmin credentials live in `config.yaml` under the `garmin:`
section (`garmin.email` / `garmin.password` — rev. 1 wrote them flat; see §13) and are
**not** read from the environment —
`config.yaml` is gitignored, and keeping them out of env avoids leaking
credentials into process listings and shell history. The measured TSS is
computed in TrainMate from Garmin's per-zone seconds via fixed Coggan/Friel
multipliers (the FTP/LTHR that define those zones live on the Garmin side), so
the GarminScraper `.env` duplication disappears. FTP/LTHR remain in
`config.yaml` `user_profile` for coaching context. Token store defaults to
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
- **Instants → stay UTC.** Moments compared against "now": `created_at`, freshness
  timestamps, `last_pull_utc`, LLM log times.

*Rev. 2 — the rule, not a site list.* Rev. 1 enumerated the call sites by file and line.
Those pointers are all dead (`db` and `coach` became packages, `cli.py` split into
`trainmate_cli.py` + `trainmate/cli/`, `google_sheets.py` was deleted), and enumerating
them was never the invariant anyway. The invariant that actually holds, and is worth
checking on review, is: **every calendar date goes through `util.today_date()` /
`util.today_str()`; every surviving `datetime.now(timezone.utc)` is an instant
comparison, never a date.** That is true across the tree today.

No configurable timezone override for now; it can be added later inside the one
helper.

---

## 13. Configuration (`config.yaml`)

```yaml
refresh_minutes: 120           # top-level: min time between automatic Garmin/Calendar refreshes

garmin:                        # rev. 2: nested — the keys are NOT flat `garmin_*`
  email: "you@example.com"     # credentials (§11); config.yaml is gitignored
  password: "your-garmin-password"
  token_dir: "~/.garminconnect"  # where garminconnect persists OAuth tokens
  mutable_days: 3              # trailing days a forward-refresh re-fetches
  backfill_prompt_days: 30     # small→auto / large→print-command cutoff (also gates interior gaps)
  initial_backfill_days: 90    # range used to build the cold-start printed command
  throttle_seconds: 0.2        # sleep between Garmin calls; --sleep overrides on `data pull`
```

Each gets a typed accessor on `Config` alongside the existing properties.

> **Rev. 2 — the keys are nested; rev. 1's YAML was silently ignored.** Rev. 1 wrote
> flat top-level keys (`garmin_mutable_days`, `garmin_email`, …). Everything Garmin-owned
> actually lives under a `garmin:` section with the prefix **stripped**, as shown above and
> in `config_template.yaml`. Only `refresh_minutes` is top-level (it gates Calendar
> syncs too, so it isn't Garmin's) — that one line rev. 1 got right. The confusion is
> easy to inherit because the Python *accessors* kept the flat names —
> `config.garmin_mutable_days` reads `garmin: mutable_days:` — so code and config do not
> spell these the same way. Copying rev. 1's block into `config.yaml` produces no error
> and no effect: unknown top-level keys are ignored and every knob silently stays at its
> default. `hr_zone_coverage_min` and the other load-model knobs live under `garmin:` too.

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
- Config knobs and defaults (§13); creds in `config.yaml`, **not** env; FTP/LTHR from
  `user_profile`. *(Rev. 2: this bullet used to read "creds via env", contradicting §11
  in the same document. §11 was and is right — there is no environment path for Garmin
  credentials. The bullet was a leftover from an earlier draft.)*

**Open / to confirm during implementation** — *resolved (rev. 2):*
- Exact helper name(s) in `util` (`today_str()` vs `local_today()`). → **both**:
  `util.today_date()` returns a `date`, `util.today_str()` the `YYYY-MM-DD` string.
- Whether `ensure_data` lives in `trainmate/garmin.py` or a thin coordinator
  module called by the CLI. → it lives in the garmin package, `garmin/sync.py`,
  alongside `pull()`; the CLI calls it at command entry.
- Whether a cron `data pull` should emit a desktop/push notification on
  `GarminAuthRequired` (currently: log + non-zero exit only). → still log-only; not
  revisited.

---

## 16. Out of Scope

- Configurable athlete timezone / travel handling.
- Web-initiated pulls or in-request auth.
- Chasing interior gaps that are legitimately empty (no-watch) days — the null-row
  convention treats them as covered.
- Any change to Google Calendar sync.
- Backfilling RPE/zone detail for very old activities Garmin has since pruned.
