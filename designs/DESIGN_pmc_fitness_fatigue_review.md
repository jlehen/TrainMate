# Review: DESIGN_pmc_fitness_fatigue.md

> **HISTORICAL — a closed record, not a live checklist.** This is a code review of
> **DESIGN_pmc_fitness_fatigue.md**, written 2026-07-04 against that design's *rev. 1*. Every
> finding below — all 8 numbered ones and every smaller one — was resolved and verified live in
> code on 2026-08-03. The file is kept for the *reasoning*, not for work still to do. Three
> things a reader must know before following anything below:
>
> 1. **The resolutions live in the design's own prose, not in a resolution map.** This header
>    used to point at "the design's §9"; revs. 6–7 restructured that document and no §9 exists.
>    Read instead: §3.3 (finding 1), §1 + Phase 2 (2), §5.2 (3), §3.4 (4), §4 (5), §5.1 (6),
>    §3.1 + §6.1 (7), §6.1 (8), and §5.5 / §3.4 / §5.4 / §8 for the smaller findings.
> 2. **The final resolution is not the one this header recorded at the time.** It says the
>    findings landed in rev. 2 and rev. 3; the design is now at **rev. 7**, and rev. 6 later
>    *cut* or *deferred* parts of what this review argued for — finding 2's projection became
>    Phase 2 (still unimplemented), and finding 8's green-TSB-band debate ended with the band
>    removed outright rather than with either option the finding proposed.
> 3. **ACWR is retired and the modules were split into packages.** ACWR went away on 2026-07-31
>    (DESIGN_load_ratio.md); findings 4(b), 6 and 7 quote ACWR code that no longer exists, and
>    those quotes are marked in place. Every module and line reference in this file also
>    predates the package split (`garmin.py` → `trainmate/garmin/*`, `coach/engine.py` and
>    `coach/service.py` → packages), and is left as written: read all file/line pointers below
>    as *where the code lived in July 2026*, not as a map of the tree today.

**Status:** Findings recorded 2026-07-04 — **all folded into the design**
(rev. 2, then refined by author decisions in rev. 3, both 2026-07-04; but see
banner note 2 — the final shape landed in revs. 6–7). Reviewed against the
design at its initial snapshot (same branch, previous commit) and against every
file it references. Findings 1–5 were blocking; 6–8 implementation decisions the
doc now makes explicitly; the rest an editing pass. This file is retained as the
historical review record — the findings below describe the *rev. 1* design.

---

## 1. Zero-seeded EWMAs feed six weeks of garbage to the LLM (blocking)

**Resolved** — accepted as written. Leading-edge blanking ships in `garmin/pmc.py` and every
surface that reads PMC; design §3.3(a).

The design seeds CTL and ATL at 0 at the start of DB history. An athlete
training a steady 60 load/day then shows, three weeks in: ATL ≈ 58 (7-day
constant, converged) but CTL ≈ 28 (42-day constant, still climbing), so
TSB ≈ −30 — exactly the science-file threshold for "excessive fatigue,
insert recovery". The ramp rate over the same warm-up reads +7..+10/week,
past the ">8 red flag" line. Nothing about the athlete's training changed;
both signals are pure seeding artifacts.

The "TrainingPeaks/intervals.icu behave the same" justification does not
transfer: those tools draw a chart for a human, who visually discounts the
ramp-in at the left edge (and both tools let you set starting CTL/ATL
manually for exactly this reason). Here the numbers land in LLM prompts next
to hard directive thresholds, and the LLM has no way to know the early
values are artifacts. The analyze/bootstrap paths are guaranteed to hit
this: their weekly digests start at the beginning of DB history, so §5.3's
`min_tsb` and `week_ramp` will narrate a phantom overreach block in every
bootstrap.

**Fix:** no seeding machinery needed. Compute from day one as designed, but
suppress (treat as None) surfaced CTL/ATL/TSB/ramp for roughly the first
1×τ_ctl days after DB history starts — in the per-day lines, the weekly
digest, and the CLI — using the same None-omission guard the design already
specifies.

## 2. "Fixes all four §1 failures" overclaims the taper fix (blocking)

**Resolved (claim softened; projection deferred)** — design §1 now claims three fixes, not four,
and calls the taper only *anchored*. The "optional middle ground" projection was deferred to the
design's Phase 2 and is still unimplemented.

The taper directive targets TSB on *event day*, weeks in the future. With
backward-only PMC the LLM gets today's values and must mentally simulate two
exponential decays across a 2–3 week taper — exactly the arithmetic the
design's own "the app computes; the LLM reasons" principle forbids
delegating. Failure #2 is only half-fixed; soften the claim. Optional cheap
middle ground for phase 1: a zero-further-training projection ("TSB on event
day if the athlete did nothing from today: +X") is pure decay math, needs no
planned-TSS decisions, and gives the LLM a hard anchor.

## 3. Ramp rate never reaches the prompts that set next week's load (blocking)

**Resolved** — the ramp line is emitted into the generate and adapt contexts as well as the data
summary; design §5.2.

The ramp line lands only in the data summary, which feeds the strategy/plan
prompts. Workout generation (engine.py ~L556) and adapt (~L792) — the
prompts that actually write and adjust weekly TSS, and the subject of
failure #3 — receive per-day metrics lines with no computed ramp. The LLM
would have to subtract CTL values seven lines apart. One-line fix: include
the ramp figure in the generate/adapt context too.

## 4. DERIVATION_PAD_DAYS = chronic is under-padded twice over (blocking)

**Resolved; (b) since moot** — the pad is now `max(28, ceil(1.5 × pmc_ctl_days))`, read live from
config (design §3.4). Part (b)'s `acwr_chronic_days` term never landed because ACWR was retired
before it was needed — see banner note 3.

(a) CTL needs ~1.5×42 ≈ 63 days of prior data to be converged at the start
of a displayed window; the 28-day pad neither pulls it nor warns (the
`_warn_manual` text only names baselines and ACWR). (b) Making
`acwr_chronic_days` configurable while keeping pad = chronic is a latent
bug: set it to 14 and the pad drops below the **hardcoded** 28-day baseline
lookback in `recompute_derived()` (`range(1, 29)`), silently corrupting
baselines near window edges. Pad should be
`max(chronic_days, 28, ~1.5 × pmc_ctl_days)` — or the doc must say why not.

## 5. wipe_garmin_data() never recomputes; EWMAs make that unbounded (blocking)

**Resolved, with a placement change** — the recompute runs at the *command* layer after the wipe,
not inside `wipe_garmin_data()` itself (circular imports and lock scope); design §4.

`db/wipes.py` deletes rows by range without calling `recompute_derived()`,
contradicting "every existing refresh path picks the new values up for
free". For ACWR the stale zone is bounded to 28 days after the wiped range;
an EWMA carries deleted load forward forever, so **every** row after the
wipe holds stale CTL/ATL/TSB until some future pull sweeps. Existing bug,
amplified by this design. Fix: call `recompute_derived()` at the end of the
wipe.

## 6. The None-guard claim misreads the current code

**Resolved** — `format_metrics_history()` builds the line field by field, every field `is not
None`-gated, and an all-NULL row renders `(no data)`; design §5.1. The ACWR field quoted below is
**since-removed code** (banner note 3) — do not go looking for it; the crash it describes was
real at the time and the fix that replaced it covers the whole line.

`format_metrics_history()` does `f"ACWR={m['acwr']:.2f}"` with no guard — a
NULL acwr **crashes** (TypeError), it is not "implicitly omitted". A
NULL-acwr row is reachable: a pull that dies between `_ingest_metrics` and
the end-of-pull recompute leaves raw rows with no derived values. Guarding
only the three new fields leaves that crash in place. Also existing wart:
missing RHR renders as `RHR=Nonebpm`. Pick one convention for the whole
line.

## 7. Status-line NULL and gap handling is unspecified; zero-fill would lie

**Resolved, both halves** — NULLs render `—` and never zero (design §6.1), and the interior d−7
gap rule is specified *and* implemented: nearest-earlier baseline, rescaled to a per-week rate,
lookback bounded (design §3.1). The `last_metrics['acwr'] or 0.0` pattern quoted below is
**since-removed code** (banner note 3), cited here only as the zero-fill precedent to avoid.

`status.py` uses `last_metrics['acwr'] or 0.0`. Copying that pattern means
post-deploy `tm status --no-pull` prints `CTL 0.0 | ATL 0.0 | TSB 0.0` —
and TSB 0.0 *looks like a meaningful neutral reading*, not missing data.
Spec: omit or dash when NULL, never zero-fill. Also unspecified: the ramp
needs the row at exactly d−7; the design covers the young-DB edge but not an
interior gap at d−7 in an older DB (nearest-earlier row vs omit — pick one).

## 8. Color band hole; green band contradicts the design's own reasoning

**Resolved, by a third option** — the bands touch (yellow 5–8, red ≥8) as asked, but rev. 6 chose
neither "uncolor it too" nor "note the inconsistency": the green band was **cut entirely**, along
with the phase machinery that gated it. Design §6.1, rationale in §7.

Ramp "`>8` red, `5–7` yellow" leaves 7.5 rendering *plain* while 6.0 renders
yellow; bands must touch (yellow 5–8, red ≥8). And the argument used to
leave TSB −30..+5 uncolored ("phase-dependent judgment belongs to the
coach") applies equally to +5..+25: sustained mid-build it means fitness is
decaying (the science file's "resume building" case), yet it renders green.
Either uncolor it too or note the accepted inconsistency.

## Smaller findings

**All resolved.** The fingerprint hashes all three PMC fields (§5.5); the params live under
`garmin:` with the staleness accepted in writing (§3.4); the span ends at `max(last activity,
last metrics)`; §4 exists; `daily_load` is ISO strings throughout; the real `recompute_derived()`
callers are named; ARCHITECTURE.md was updated; the weekly-digest ramp has a straddle guard
(§5.4); and every listed missing test exists (§8).

- **Double cache invalidation.** Adding `tsb` to `met_digest` changes tuple
  arity, shifting every fingerprint at deploy (values still NULL); the first
  recompute shifts them again. Collapses to one refresh on the normal path,
  but a `--no-pull` analysis in between pays for two LLM re-runs. Also: the
  prompt now carries CTL and ATL per day but only TSB is hashed; hashing all
  three costs nothing and matches the stated principle.
- **Config-change staleness.** The rewriting sweep only runs on the next
  *pull*; between a τ edit and that pull, all displayed values reflect the
  old constants, silently. Also unspecified: `garmin:` vs `coach:` section
  for the four params (precedents both ways). The `garmin.py` "Standard
  constants, not tunables" comment becomes false.
- **Span end contradicts §3's own text.** Span ends at the last *metrics*
  date, but activities-only pulls (`pull(metrics=False)`) can leave trailing
  activity days beyond it — which are then NOT walked, despite §3's promise.
  End at `max(last activity, last metrics)`.
- **No §4 header.** Sections run 1, 2, 3, 5, 6, 7, 8; the storage bullets
  (ALTER TABLE / types.py / save_metric_cache) dangle under the
  "Configurable windows" subsection of §3.
- **Type inconsistencies.** Prose says `daily_load: Dict[date, float]`, the
  signature says `Dict[str, float]`, params are `date` objects; the real
  code uses ISO strings. Pick one.
- **"zone-rehierarchy funnels through recompute_derived()"** — the only
  callers are `pull()` and `backfill_tss()`. Name the real paths.
- **ARCHITECTURE.md** is absent from the rollout plan; AGENTS.md requires
  updating it (derived-metrics flow ~L132, wipe/analysis-cache ~L478).
- **Weekly digest ramp crosses week boundaries.** `end_ctl − CTL 7 days
  earlier` reaches into the previous week's slice; the first week of an
  analysis window has no prior week (None-guard), and is also exactly where
  finding #1's warm-up garbage lives.
- **Missing tests:** empty DB (degenerate span — `min()` over no dates);
  COALESCE regression (a metrics-only re-pull must not null stored PMC —
  the design relies on it, nothing pins it); interior d−7 gap for ramp;
  status-line NULL rendering.

## What holds up well

The §2 touchpoint inventory is accurate down to line numbers (verified).
Walking calendar days instead of metrics rows correctly decays through rest
days. The TSB off-by-one is specified *and* pinned with a test. "Upsert onto
existing rows only" preserves the cache's one-row-per-Garmin-day meaning.
