# Design: Backward Evaluation as Forward Context

**Status:** Draft · **Date:** 2026-06-07 · **Branch:**
`reconcile-memory-strategy-vs-macrocycle`

**Implementation status (2026-06-07) — COMPLETE (tests passing).** All pieces
landed: the `analysis_cache` table + CRUD (§5.1); `CoachEngine
._get_evidence_fingerprint()` with the deliberate prompt/science omission (§5,
§11); the `apply_learning_deltas(..., suppress_reinforcement=)` integrity
invariant (§8); `workout generate` made read-only (§11); the `analyze` reuse path
(fingerprint → cache → reuse-or-recompute, with `suppress_reinforcement` on forced
unchanged re-runs); the `--inspect-only`/`--force` CLI flags (§9); `plan feedback
--edit` (§7); and the planned-vs-inferred review via **Option A** (§6, §7 — see
the note below).

> **AS BUILT (2026-08-04) — read this before the sections below.** Three things
> drifted after this document was written, and they change the command surface and
> the write topology rather than the mechanism:
>
> 1. **`data analyze` no longer exists.** It split into **`data bootstrap`** (alias
>    `b`, horizon `long`, once per onboarding, establishes the reflect watermark)
>    and **`data reflect`** (horizon `short`, incremental from the watermark).
>    Both share `CoachService._run_workout_analysis()`. Read every "`data analyze`"
>    below as "the analysis flow (`data bootstrap` / `data reflect`)"; the
>    reconstruction that `plan generate` replays is the one `data bootstrap`
>    caches. See ARCHITECTURE §"data bootstrap / data reflect" and
>    DESIGN_evidence_based_confidence.md.
> 2. **`plan generate` never became a learnings writer, nor a feedback writer.**
>    The only learnings writer is the analysis flow; `workout adapt` was made
>    read-only too. Every "`plan generate` **Writes**" claim below (§3, §4 table,
>    §8 opening, §10) is **superseded** — see DESIGN_evidence_based_confidence.md
>    §6 *AS BUILT*.
> 3. **§8's `suppress_reinforcement` flag was never shipped and is now
>    unnecessary.** The soundness invariant it protected — "reinforcement tracks
>    new evidence, not new invocations" — is instead enforced structurally by the
>    per-learning **evidence-week basis** (DESIGN_evidence_based_confidence.md
>    §6/§8): cited weeks are deduped against the learning's existing basis, so
>    re-citing a counted week neither raises confidence nor refreshes
>    `last_reinforced_at`. §8 below is retained for its rationale only.

**Option A chosen for §6/§7 (planned-vs-inferred).** During implementation the
§7 *auto-write into the `feedback` field* collided with the data model: every
`plan generate` writes a **new** macrocycle version (`db.save_macrocycle()` marks
the previous one `superseded` and inserts a fresh row — DESIGN_plan_rollback.md),
and that new active row starts with an empty `feedback`, so feedback never carries
across a replan — there is no stable field to overwrite. Resolution (**Option A**):
the planned-vs-actual review is built read-only by
`CoachService._build_prior_training_context()` (anchored on the elapsed mesocycle
windows of the prior plan *and* of the currently governing one — see §6 — augmented
with the cached reconstruction's insights, reused without a new LLM call),
**injected into the strategy prompt and displayed**, and writes to *no* `feedback`
field. The §7 auto-write / 4-step-ordering subsection below is therefore
**superseded** and retained only for rationale — as are the other places that
assert the auto-write (§2 goal 4, §3 "Owned by `plan generate`", §4 table
"diff → feedback", §6 closing, §10 second bullet). `plan feedback --edit` was the
human's path to that field; the field itself is gone since — feedback is now an
append-only log the regeneration reads whole, and `--edit` retired with the slot it
curated (DESIGN_plan_feedback.md).

This document captures the design for feeding *backward-looking* analysis of
past training into *forward-looking* decisions (planning and workout
generation). It supersedes the relevant TODO items:

- "Provide previous macro- and meso-cycles when adapting or generating workouts?"
- "`data analyze` or `data adapt` saves to memory?"
- "Automatic data analysis: over a longer period when planning macrocycles and
  mesocycles; over a shorter period when planning microcycles and workouts."

---

## 1. Motivation

The analysis flow (`data analyze`; as built, `data bootstrap` / `data reflect`)
already reverse-engineers past training into a rich result —
`{macrocycle_summary, inferred_macrocycle, inferred_mesocycles[],
physiological_insights[], learning_updates[]}` (see ARCHITECTURE §3, §10).
`learning_updates` survives as coach-learnings deltas, and the reconstruction —
its summary, reverse-engineered macro/mesocycle structure, and physiological
insights — is cached and replayed read-only into `plan generate`'s strategy
prompt (Option A, see §5/§6) and into the progress timeline (§5.1); none of it is
discarded.

The reconstruction is exactly the context that would ground a *new* plan in the
athlete's demonstrated reality rather than an idealized template, and would tell
workout generation how the athlete has been responding lately. The goal is to
feed that backward evaluation forward — without corrupting the coach-learnings
memory model in the process.

---

## 2. Goals / Non-Goals

**Goals**
- Reuse backward analysis as context for `plan generate` (long horizon) and
  `workout generate` (short horizon).
- Compare what was *planned* against what *actually happened*, at both the
  macrocycle and mesocycle level.
- Keep coach-learnings reinforcement *sound* under repeated/curiosity runs.
- Reuse existing machinery (coach-learnings deltas, the `*_hash` reuse idiom,
  the macro/meso `feedback` fields) rather than inventing parallel channels.
  **Superseded (Option A):** the `feedback` half of this goal was dropped — the
  review is prompt context, not a stored field. The deltas and the hash idiom
  were reused as stated.

**Non-Goals**
- No backward evaluation in `workout generate` (see §4 *AS BUILT*) — it consumes
  raw recent metrics + PMC directly and runs no LLM backward pass.
- No backward evaluation in `workout adapt` (see §4) — it runs daily; the cost
  is not justified.
- ~~No per-learning evidence provenance in v1 (see §7, "Edges") — deferred as
  YAGNI.~~ **Superseded (DESIGN_evidence_based_confidence.md):** per-learning
  evidence provenance *was* built (the evidence-week basis), and it is what now
  supplies §8's integrity invariant.
- ~~The human-facing narrative `macrocycle_summary` is *not* fed to the LLM (it is
  useful to read, but not decision-relevant for the model).~~ **Superseded
  (Option A):** the summary *is* replayed into the strategy prompt — it is the
  cheapest carrier of the prior arc's narrative, and §1 always said so.

---

## 3. Core Concept

A **backward evaluation** is an LLM pass over past completed activities + metrics
that reconstructs/assesses what actually happened. It serves two distinct jobs
that must not be conflated:

- **Durable memory formation** — distilling general, cross-block,
  decaying observations into `coach_learnings` ("responds badly to consecutive
  hard days"). Slow-moving. Owned by `data analyze` (as built: `data bootstrap` /
  `data reflect` — the *only* learnings writers).
- **Situational assessment** — a point-in-time judgement about a *specific* past
  period ("*this* base block did not build base"), consumed immediately by the
  plan it informs and written to that block's `feedback`. Owned by
  `plan generate`. **Superseded (Option A):** the assessment is real and is owned
  by `plan generate`, but it is *built and shown*, never written — it lands in the
  strategy prompt instead of in `feedback`.

Both look backward; they produce *different products*. §9 explains why running
them back-to-back is not redundant double-counting.

---

## 4. Where Backward Evaluation Runs

"Backward eval" = an LLM pass that reconstructs/assesses past training. The
frequency column is load-bearing: cost tracks frequency, so inline evaluation is
affordable exactly where it is rare and is excluded where it is frequent.

| Command            | Backward eval? | Horizon            | Product & use                                                       | Learnings  | Frequency  |
|--------------------|----------------|--------------------|---------------------------------------------------------------------|------------|------------|
| `data analyze`     | Yes — curator  | Long (auto)        | Full reconstruction (cycles + insights). The deliberate "take stock"| **Writes** | Occasional |
| `plan generate`    | Yes — inline   | Long (~last macro) | Reconstruction **+ planned-vs-inferred diff** → feedback; new plan   | **Writes** | Rare       |
| `workout generate` | Yes — inline   | Short (~4–6 wk)    | *Recent-response insights only* — no cycle reconstruction            | Read-only  | Occasional |
| `workout adapt`    | **No**         | —                  | Excluded; already uses raw recent metrics + adherence + learnings    | —          | Daily      |

> **AS BUILT — the table above is the original intent; this is what shipped.**
>
> | Command            | Backward eval? | Horizon slot | Product & use                                                        | Learnings  | Frequency  |
> |--------------------|----------------|--------------|----------------------------------------------------------------------|------------|------------|
> | `data bootstrap`   | Yes — curator  | `long`       | Full reconstruction (cycles + insights); cached and replayed forward  | **Writes** | Once       |
> | `data reflect`     | Yes — curator  | `short`      | Recent-response read (insights, no cycles — §10.3); window runs from the watermark to the last completed week (§10.4) | **Writes** | Occasional |
> | `plan generate`    | **No LLM pass**| —            | *Reads* the cached `long` reconstruction + a computed planned-vs-actual review into the prompt (§6) | Read-only | Rare |
> | `workout generate` | **No**         | —            | Never built. Raw metrics + PMC over `metrics_lookback_days`; no backward pass, no cache access | Read-only | Occasional |
> | `workout adapt`    | **No**         | —            | Excluded as designed; also made read-only w.r.t. learnings            | Read-only  | Daily      |

**Horizon dictates the product, not just the window size.**
- Long horizon → cycles + insights + planned-vs-inferred diff.
- Short horizon → physiological / recent-response read only. *Cycle
  reconstruction is meaningless below the long horizon* — you cannot infer a
  periodization structure from four weeks.

> **AS BUILT (rev 1) — the horizon selected the cache slot, not the product.** Both
> commands ran the *identical* prompt through `_run_workout_analysis()`; `horizon`
> picked which `analysis_cache` row was written and the label was used only for LLM
> logging, so `data reflect` also emitted `inferred_macrocycle` /
> `inferred_mesocycles` over its short window. Splitting the prompt by horizon
> stayed unbuilt because **nothing consumed the short reconstruction** — a weaker
> claim written to a slot no one read costs nothing but tokens.
>
> **AS BUILT (rev 2) — built, once that premise expired.** §10.2 wired the short
> slot into the strategy prompt, at which point reflect's cycle-guessing stopped
> being inert and started shaping plans: a five-day window would name a
> "macrocycle" whose own focus text conceded the surrounding period was unknown.
> `_data_analyze_logic` now takes `horizon` and branches on it (§10.3), so the
> paragraph above this box is the behaviour rather than the intent.

---

## 5. The Reconstruction Artifact (persist + reuse)

The reconstruction is a **cached artifact keyed by an evidence fingerprint**, not
a throwaway. This is what lets `plan generate` reuse `data analyze`'s work
instead of paying for a second backward pass (see §9).

- **Evidence fingerprint** — a hash of the *inputs* to the analysis: the set of
  completed-activity IDs + metrics within the window (plus the window itself).
  This follows the existing `goals_hash` / `lifeevents_hash` / `config_hash`
  reuse idiom (ARCHITECTURE §10). **As built the input list is wider**, because
  everything the analysis prompt is shown must move the hash: per activity the
  load-bearing *fields* too (`date`, type, `duration_sec`, `tss`, `rpe`,
  `zone1..5_sec`) so a corrected re-pull invalidates; the overlapping
  **constraints** (DESIGN_constraints.md §6); and the ingested **daily-context**
  signals (DESIGN_calendar_context_ingest.md §7). The deliberate omissions are
  unchanged — see §11.
- **Cache key** = (window/horizon, evidence fingerprint). Hashing the concrete
  activity-id set — not merely the date range — narrows the overlapping-window
  edge (see §7).
- `data analyze` produces/refreshes the artifact and curates learnings.
- `plan generate` consumes the matching artifact when the fingerprint is current,
  recomputing only if stale or absent. **Superseded (Option A):** `plan generate`
  performs no LLM call and therefore *cannot* recompute — it reads whatever sits
  in the `long` slot, unconditionally. The fingerprint check lives only in the
  analysis flow that writes the slot. Consequence, accepted: if `data bootstrap`
  has not been re-run in months, a months-old reconstruction is replayed verbatim.
  The window it covers is printed alongside it (`INFERRED FROM PAST TRAINING
  (start..end)`) so the staleness is visible rather than silent, and `plan
  generate`/`status` nudge toward `data bootstrap` on a cold start.

The fingerprint plays **two independent roles** that must be kept separate:

1. **Cheap-skip / reuse** (an optimization): unchanged fingerprint → reuse the
   stored reconstruction, skip the LLM call. *Bypassable* with `--force`.
2. **Integrity invariant** (a correctness property): on unchanged evidence,
   suppress spurious reinforcement. *Not* bypassable by `--force`. See §8.

### 5.1 Storage — dedicated cache table

The artifact lives in its own SQLite table (chosen over attaching to
`macrocycles`, an inferred-cycle discriminator, a generic blob, or on-disk JSON;
see §11 for why). The deciding constraint: `data analyze` can run **standalone —
no plan, possibly no objective** — so the store cannot hang off the plan. Keeping
it in `trainmate.db` honours the single-source-of-truth convention and gives
transactional consistency with the learnings written in the same flow.

The whole reconstruction is stored as one JSON TEXT blob (like macrocycle
`strategy`), not exploded into typed columns — nothing queries *inside* it; it is
fetched whole, fed to a prompt, or rendered.

```
analysis_cache
| Column           | Type       | Notes                                          |
|------------------|------------|------------------------------------------------|
| id               | INTEGER PK |                                                |
| horizon          | TEXT       | 'long' | 'short' — cache slot (see retention)  |
| fingerprint      | TEXT       | hash of activity-id set + metrics + window     |
| window_start     | TEXT       | YYYY-MM-DD                                      |
| window_end       | TEXT       | YYYY-MM-DD                                      |
| reconstruction   | TEXT       | JSON: inferred cycles + physiological insights |
| created_at       | TEXT       | ISO timestamp                                  |
```

**Retention: one row per `horizon`** (an upsert keyed on `horizon`). A new
`data pull` shifts the fingerprint and overwrites the slot — we only ever want
the *current* reconstruction. A fingerprint-keyed *history* (so `--inspect-only` over
an old window hits cache) is deferred until missed.

`db.py` methods (fresh-connection-per-call, per ARCHITECTURE §4):

- `save_analysis_cache(horizon, fingerprint, window_start, window_end,
  reconstruction)` — upsert on `horizon`.
- `get_analysis_cache(horizon)` → row or `None`. Callers compare the stored
  `fingerprint` against the freshly computed one to decide reuse vs recompute.
- `wipe_analysis_cache()` — for `data wipe` symmetry. It is what
  `wipe_garmin_data()` calls to clear the slots: the reconstructions are derived
  from exactly the evidence that wipe deletes, so they are meaningless afterwards.

Reuse decision (in `CoachService`): compute the current fingerprint, call
`get_analysis_cache(horizon)`; reuse when fingerprints match and `--force` is
absent, else recompute and `save_analysis_cache(...)`.

**Forward consumers of the `long` slot** (all read-only, none recomputes):

1. `CoachService._build_prior_training_context()` → the strategy prompt (§6).
2. `trainmate/timeline.py` → `progression.assemble_timeline()`, which draws the
   inferred mesocycle blocks as `~`-prefixed bands wherever no planned block
   covers the span (`progress timeline` and the web dashboard's read-only view).
   Added later by DESIGN_progress_timeline.md §6.1; the sections below that call
   the strategy prompt the *only* consumer predate it.
3. `data show-analysis` (`san`) → `cli/data.py:_render_analysis_report`, the same
   renderer `bootstrap`/`reflect` print through. It exists because the two ways to
   *read* a stored reconstruction before it were both indirect: the timeline shows
   block names only, and `bootstrap --inspect-only` — read-only as to writes — still
   pays for a fresh LLM pass the moment the fingerprint has moved, so "show me what
   is stored" could silently become "recompute it". `--short` addresses the other
   slot. Retention being one-row-per-horizon, it reports the current picture and
   flags activities that post-date the slot's window rather than implying currency.

---

## 6. Planned-vs-Inferred Diff

The single most valuable planning input: you hold two representations of the same
past period — the **planned** macrocycle/mesocycles (in the DB) and the
**inferred** ones (from reconstruction). Nobody currently subtracts them at the
cycle level (`adherence.py` diffs individual workouts, not blocks).

**Reconstruction is the base layer; the diff is an optional overlay.** There is
no special "first macrocycle" branch:

- **No prior plan** → inferred cycles stand alone ("here is the block structure
  your training fell into"). The overlay is simply empty.
- **Prior plan exists** → overlay the comparison *wherever planned data covers* —
  per-segment, not all-or-nothing. The first-macrocycle case is the degenerate
  end of that spectrum (zero coverage) and falls out for free.

**Mesocycle comparison — anchor on planned windows.** Planned and inferred
mesocycle boundaries will never line up, so do *not* try to reconcile two
boundary sets. Instead take each *planned* mesocycle window as the anchor and ask
"did the actual training in this window match the stated `focus`?" (intent
fidelity). This sidesteps the alignment problem entirely. The inferred view is
then used to flag blocks that *emerged outside* what was planned. Timing fidelity
("did transitions happen when planned?") is secondary and noisier — defer it.

Diff output has a natural home in existing fields: macro diff →
`macrocycle.feedback`, per-block diff → `mesocycle.feedback`.

> **AS BUILT (Option A).** Nothing is written. The review is assembled read-only by
> `CoachService._build_prior_training_context()`, printed, and injected into the
> strategy prompt as `PRIOR TRAINING REVIEW`. Two refinements over the text above:
>
> - **The anchor is wider than "the prior plan".** Elapsed blocks of the prior
>   macrocycle *and* of the currently governing one are both walked: drift
>   diagnosed only one macrocycle late is history, not a finding
>   (DESIGN_intensity_distribution.md §3, gap 2).
> - **The per-block comparison is quantitative, not just intent-fidelity.** Each
>   planned block shows its stated `focus` beside what the athlete's sessions
>   actually measured — volume, load, and the **per-sport per-zone intensity
>   distribution as a per-week rate, with the delta against the preceding block**
>   (DESIGN_intensity_distribution.md §4.1/§9). That delta is the intensity-creep
>   check: weekly TSS can hold flat while easy volume quietly gives way to tempo.
> - **Adherence joins intent fidelity.** The measured half alone cannot say whether
>   a block was *carried out*: 545 TSS over four weeks reads identically whether it
>   was 100% or 50% of what the plan asked, so a half-missed block looked exactly
>   like a completed one and the next macrocycle ramped from a load the athlete
>   never reached. Each elapsed block therefore also carries **one line per week —
>   planned load beside produced load** — from `progression.weekly_aggregates`, the
>   same computation `tm progress` renders, so the coach and the athlete can never
>   read different numbers for the same week. The in-progress week states raw load
>   beside its elapsed day count and is never extrapolated
>   (DESIGN_intensity_distribution.md §9.3).
> - **What the plan PRESCRIBED sits beside what was measured**
>   (DESIGN_intensity_distribution.md §9.2a). Which of the two an athlete diverged
>   from decides whose problem it is, and only the periodization consumer may act
>   on the mis-designed case.
>   The cached reconstruction's summary/cycles/insights are appended below it.

### 6.1 Which macrocycles the review walks, and in what order

`_build_prior_training_context` takes a **list** of macrocycles and adds the
governing one itself. Two things drove that, and the second turned out to matter
more than the first.

**The list.** `plan_generate` derives two different macrocycles and they are not
interchangeable:

- `preceding_macro` — the latest goal *before* this one that has a plan. Found by
  the `get_preceding_objectives` loop.
- `prev_macro` — the plan this generation *replaces*, which on a re-plan is the
  goal's own. It feeds the singular `PREVIOUS PERIODIZATION STRATEGY` block, which
  is about the intent the new plan departs from, so singular is right there.

The split between the two is what makes `plan generate --fresh` a coherent option
rather than an amnesia switch. Asking for a clean slate withholds the
`PREVIOUS PERIODIZATION STRATEGY` block and its continuity instruction — the athlete
is saying *don't build on that intent* — while `prev_macro` still reaches the review
below, because what they actually trained under the old plan is evidence, not intent,
and a plan written blind to it would be the idealized template §6 exists to prevent.

`prev_macro` used to be assigned over the top of `preceding_macro`, so only one of
the two ever reached the review. In practice the loss was **narrower than it looks**:
the review also adds `get_governing_macrocycle()` — the *earliest* active goal with
a plan — which usually backfills the one that was dropped. It genuinely goes missing
only with three or more planned goals in the chain, where the middle one is neither
the earliest nor the one being replanned. The fix is still worth making (the two
names now mean what they say), but it is not the common case.

**The order.** This is the one that bit. The blocks of every macrocycle are
flattened into a single list, and `block_report`'s delta baseline is
`blocks[i - 1]` — the block before it. The list was built in *argument* order, and
argument order is not chronological: on a re-plan of a later goal the pair is
`[the replaced plan, the governing plan]`, and the governing plan is the **earlier**
one. So the flattened list ran later-blocks-then-earlier-blocks, and every delta
compared a block against one that happened *after* it. The first block of the later
plan also silently got no delta at all.

Macrocycles are therefore sorted by their first block's start date before
flattening. Sorting the chosen lineages is not the same as the date-ordered
mesocycle *query* §6's implementation notes forbid: that query drags in superseded
rollback versions, whereas this only orders macrocycles the caller already picked.

**Both used to be bounded by goal status, and that was the deeper bug.** Every
accessor in this paragraph — `get_preceding_objectives`,
`get_governing_macrocycle` — filtered `status = 'active'`. A goal the athlete had
marked `completed` therefore took its whole macrocycle out of the review, which is
exactly backwards: a goal that has been *raced* is the single most informative
thing the next plan could look at. Getting the macrocycle list right (above) would
have bought very little while the goals feeding it were being filtered out on the
way in.

That filter is gone; §12 records why and what replaced it. What still bounds the
walk is `get_preceding_objectives`' `coach.goals_lookback_days` (90), which is a
deliberate recency window on the plan-start computation rather than a statement
about which goals count as history.

---

## 7. Feedback Auto-Write — SUPERSEDED (Option A)

*Retained for rationale only: nothing auto-writes `feedback`. `plan feedback --edit`
(below) shipped and was the only writer besides `plan feedback <text>`; both the
`feedback` columns and `--edit` have since been replaced by an append-only log
(DESIGN_plan_feedback.md).*

The diff/assessment is **auto-written** into the `feedback` fields. The field is
single-voice, last-write-wins, but **never silently** overwritten.

**Lifecycle.** A `feedback` field's job ends at `plan generate`: its whole
purpose is "notes for the next replanning," so the moment it is consumed into a
plan it is free to hold the assessment of the block that just closed. Overwriting
is not destroying useful state — it is turning the field over at the exact moment
its contents were used.

**Ordering within a single `plan generate`** (this is what makes it safe):

1. **Read** existing feedback and feed it into the planning prompt — the human's
   notes still influence the plan that will replace them.
2. Generate the plan + the backward assessment.
3. **Show** the existing feedback to the user (so nothing vanishes unseen).
4. **Overwrite** the field with the new assessment.

**`plan feedback --edit`.** New flag: takes no text argument; opens `$EDITOR`
seeded with the current feedback, saves on close (cf. `git commit --edit`,
`crontab -e`). Works with `--macro` or `--meso ID`. This is the human's recourse
to re-add anything shown in step 3, or to curate the auto-written text by hand.

---

## 8. Coach Learnings: Sound Reinforcement

> **AS BUILT — the invariant holds, the mechanism changed.** The
> `suppress_reinforcement` flag described below was never shipped;
> `apply_learning_deltas(deltas, available_weeks=None, source="reflect")` has no
> such parameter. Its job was taken over — better — by the per-learning
> **evidence-week basis** of DESIGN_evidence_based_confidence.md §6/§8: the LLM
> cites the `week_commencing` weeks an observation rests on, those weeks are
> validated against the analysed window and deduped against the learning's existing
> basis, confidence is *recomputed* from the resulting distinct-week count, and
> `last_reinforced_at` is refreshed only when a genuinely new week lands. So
> re-reading the same window is a no-op by construction — no flag has to detect it,
> and the "shrinking/overlapping window" edge admitted below is closed rather than
> deferred. A `contradict` op (negative evidence) was added alongside. Read the rest
> of this section as the statement of the *problem* and of the invariant, not of the
> implementation.

Adding writers (`plan generate` now contributes learnings too) and allowing
curiosity re-runs surfaces a soundness bug in `reinforce`. (`plan generate` never
became a writer — see the AS BUILT note at the top of this document; the
curiosity-re-run half of the motivation is real and is what the flow still faces.)

**The bug.** Reinforcement must track *new evidence*, not *new invocations*.
Today `reinforce` fires per call, so re-reading the same window — back-to-back
runs, or `analyze` repeated out of curiosity — inflates confidence and refreshes
`last_reinforced_at`, quietly corrupting the dormancy model (ARCHITECTURE §3): a
learning could stay "fresh" forever merely because you keep looking at it.

**The principle.** A learning is reinforced precisely when it is re-derived from
a *changed* evidence set. The guard and the correct definition are the same
thing.

**The mechanism — the evidence fingerprint (§5), used op-specifically.** On
*unchanged* evidence:

- **`reinforce` → suppressed** (and the recency-refresh side effect of
  `revise`). This is the only purely-ratcheting op, hence the only spurious one.
- **`add` / `retire` / content-`revise` → allowed.** If an improved prompt
  surfaces a learning missed before, or corrects wording, that is genuinely new
  *knowledge* from the same evidence — not a double-count.

This composes with `apply_learning_deltas`, which already runs all ops in one
transaction and skips malformed deltas.

**Edges (accepted).** The fingerprint is clean for *identical* windows and for
*growth* (new activities arrive → fingerprint shifts → reinforcement fires
correctly). The soft spot is a *shrinking/overlapping* window (analyze Jan–Apr,
then Jan–Mar), which could reinforce off an already-counted subset. Hashing the
concrete activity-id set narrows this cheaply. The precise fix — per-learning
evidence provenance — is **deferred as YAGNI** until the pain is felt.

---

## 9. Two Flags: `--inspect-only` and `--force`

The two flags answer **independent** questions, so all four combinations are
valid:

- **`--inspect-only`** — the *write* axis: "touch memory, or not?" Read-only; shows the
  reconstruction but writes nothing (no learnings, no feedback). For curiosity.
- **`--force`** — the *recompute* axis: "re-run the LLM, or reuse?" Bypasses the
  cheap-skip/reuse (§5, role 1) — e.g. after improving the prompt or science
  files and wanting a fresh reconstruction over unchanged data.

|                  | write           | read-only          |
|------------------|-----------------|--------------------|
| **reuse**        | (default)       | `--inspect-only`   |
| **recompute**    | `--force`       | `--force --inspect-only`|

Both flags are registered on `data bootstrap` and `data reflect` (as built; the
document says `data analyze` throughout).

**Critical guardrail.** `--force` bypasses the *reuse optimization*, **never the
integrity invariant** (§8). Forcing a re-run over unchanged data still suppresses
spurious `reinforce`s; it only lets you *see* a fresh reconstruction and pick up
genuinely new `add`/`retire`. `--force` is never a license to double-count. As
built this falls out of the evidence-week basis rather than a flag: a forced
re-run re-cites weeks already in each learning's basis, which dedup to nothing.

The asymmetry to remember: `plan generate -f` is harmless because a plan is
*replaced* (force = redo). Learnings *ratchet* (force-applying = double-count) —
which is exactly why the two layers must stay distinct.

---

## 10. Why Two Commands Is Not Redundant

`data analyze` and `plan generate` both look backward — is that "looking back
twice"? No:

- They produce **different products**: durable cross-block *memory* (learnings)
  vs. a situational *this-block* assessment (feedback) + a new plan.
- The earlier worry — "`analyze` creates feedback for `plan generate`" — is moot:
  `plan generate` now auto-writes its *own* feedback (§7). **Superseded
  (Option A):** the worry is moot for a simpler reason — nothing writes feedback
  at all, so there is no channel to contend over. `plan generate`'s assessment
  lives in its own prompt and dies with the run.
- The **persisted artifact (§5) removes the redundant second pass**: when
  `analyze` has already reconstructed the current evidence, `plan generate`
  *reuses* that artifact for its diff instead of recomputing. **As built this is
  absolute**, not opportunistic: `plan generate` has no recompute path, so if the
  artifact is absent the review simply carries the computed planned-vs-actual
  tables without a reconstruction (and the CLI nudges toward `data bootstrap`).
- The fingerprint invariant (§8) makes back-to-back runs harmless regardless.

`data analyze` earns a separate command because you sometimes look backward
*without* wanting a new plan — to curate memory on its own cadence, or simply out
of curiosity (especially early on). That is `--inspect-only`'s reason to exist.
As built the argument got stronger, not weaker: with `plan generate` reduced to a
pure consumer, the analysis flow is the *only* place a backward pass happens, and
splitting it into `bootstrap` (once, full backlog) and `reflect` (incremental from
the watermark) is what keeps repeated curiosity runs from re-counting history.

### 10.1 Both halves of the analysis reach the plan prompt

The analysis flow produces two things, and for a long time `plan generate`
received only one of them:

| product | where it lands | reached the plan prompt |
| --- | --- | --- |
| the reconstruction (`macrocycle_summary`, `inferred_*`, `physiological_insights`) | `analysis_cache`, replayed by `_build_prior_training_context` | yes |
| `learning_updates` → the `coach_learnings` table | rendered by `_get_learnings_text()` | **no** |

The cause was structural rather than deliberate. Every other coach call composes
its system prompt through `engine._build_system_prompt`, which carries a
`COACH LEARNINGS …` section; `_plan_generate_strategy` composes its own and
simply had no `learnings` parameter. So an established observation like *"responds
poorly to back-to-back threshold days"* shaped every individual session and never
the block structure that schedules them — and `plan generate` closed by nudging
the athlete toward `data bootstrap`, whose durable output it would not read.

**`_plan_generate_strategy` now takes `learnings` and renders it** as
`ATHLETE-SPECIFIC OBSERVATIONS`, placed directly after `PRIOR TRAINING REVIEW`:
the two are the distilled and narrative halves of the same evidence, and they read
better together than apart.

**Why not just call `_build_system_prompt` and delete the duplication?** Because
that builder states the ACTIVE strategy and mesocycle list as settled fact under
"Established Training Strategy" — which is the very artifact this call produces.
The plan prompt deliberately shows the *previous* strategy instead, as context to
build on or depart from (§6). Feeding it the current one would be circular. The
divergence is the point, so the docstring says so; the fix is the missing section,
not a merge.

The section states that the observations are **input only** here. Authoring stays
with the analysis flow (§11), and the plan response schema has no
`learning_updates` field — the wording exists to stop the model echoing them back
into `strategy` prose.

### 10.2 `data reflect`'s reconstruction is replayed too

`data bootstrap` caches under horizon `long`, `data reflect` under `short` — and
nothing read `short`. Reflect's reconstruction was written and never used; its only
lasting effect was its learnings deltas, which §10.1 had just established were not
reaching the plan either.

The staleness warning made this sharp rather than merely wasteful. It read the
`long` window and said:

> The training-history reconstruction ends 2026-05-01 (96 days ago); sessions
> since then did not shape this plan. Run `data reflect` first to bring it up to
> date.

`data reflect` cannot bring `long` up to date. Only `data bootstrap --force` can,
and bootstrap is deliberately once-per-onboarding and confirmation-gated (§9). An
athlete following the advice exactly would see the same warning forever. In effect
the plan prompt's view of "what was figured out" was **frozen at onboarding**.

**`CoachService._cached_reconstructions()` is now the single accessor** for both
what the prompt replays and how far behind it is. It returns bootstrap's row, then
reflect's when reflect's window *begins* after bootstrap's ends. Each row carries a
`label` (`full history reconstruction` / `most recent reflection`) so the prompt
names which command produced which window rather than presenting one undated
blur.

The gate tests the window's **start**, not its end. Ending later is the wrong
question: a window that re-reads bootstrap's weeks on its way past them still puts
one body of evidence in front of the model twice, and the model has no way to
discount the second copy. Testing the start admits only genuinely new ground.
Normal operation is unaffected — the watermark puts reflect's start the day after
bootstrap's end — so this bites exactly the two cases that create overlap: an
explicit `data reflect -d` reaching backwards, and a re-run of `bootstrap` that
rewinds the watermark. The cost is that a *partially* overlapping window is
dropped whole, tail included; the recovery is a reflect without `-d`. Simplicity
wins here over a rule that would have to reason about which prose sentence
described which week.

`_maybe_warn_stale_analysis` judges the lag over the same rows, taking the latest
window end. The warning can therefore never name a window the prompt did not read,
and the command it points at is one that can actually clear it. Its wording names
the *training history read into the plan* rather than "the reconstruction": past
§10.3 only bootstrap's row carries cycles, and bootstrap being months old is the
design (once per onboarding) rather than news worth a warning.

The two reconstructions are **complementary, not competing**: bootstrap's is the
long arc, reflect's is the recent slice, and they are shown as two sections with
their own date spans rather than merged. Only one `short` row is ever retained
(one row per horizon, upsert), so "most recent reflection" is literal.

### 10.3 The horizon finally selects the question

§4 always said the horizon should dictate the *product*. §10.2 is what forced it:
once reflect's slot was replayed forward, the structure it invented from a handful
of days was no longer inert. `_data_analyze_logic` therefore takes `horizon` and
branches in three places — the TASK opener, the `## RESPONSE FORMAT` schema, and
the system prompt's one-line role.

| | `long` (`data bootstrap`) | `short` (`data reflect`) |
|---|---|---|
| asks for | the periodization phases that occurred | how the athlete *responded* |
| schema | all five fields | summary, insights, learning updates |
| forbids | — | inferring macro/mesocycles, explicitly |

Reflect keeps `macrocycle_summary` despite the name being a poor fit for a short
window: renaming it would mean teaching the renderer, the strategy prompt and the
cache reader two spellings for one field, which costs more than the wart.

Nothing downstream needed defending. `_render_analysis_report` already guards each
section with a presence check, `_reconstruction_lines` iterates `… or []`, and
`timeline.py` only ever reads the `long` slot. A field that stops being emitted
simply stops being rendered.

### 10.4 A window ends on a completed week

The evidence basis counts **whole weeks** (DESIGN_evidence_based_confidence.md §4),
and `available_weeks` is every Monday the window touches — partial ones included.
A window ending mid-week therefore let a part-week be cited as a whole one, and the
`UNIQUE(learning_id, week_commencing, polarity)` dedup that protects against
re-counting then *prevented* the rest of that week from ever topping it up. Reflect
every Wednesday and each week's citation rests on three days; reflect daily and it
rests on one. Confidence would climb per calendar week on a seventh of the evidence
— not double-counting, but arriving at the same wrong place.

`data reflect` now ends its window at the last completed Sunday unless an explicit
end date is given. Two consequences, both wanted:

1. Every citable week is a whole week.
2. A run with no completed week since the watermark takes the existing
   "nothing new" path — **no LLM call, no cost**. This is what makes a daily
   invocation harmless rather than merely tolerated, and it lets a weekly cadence
   emerge from the data model instead of from the athlete's memory.

An explicit `--until` is honoured as given: the snap is a default, not a policy.
Because a snapped end can now fall before an explicit start, the
start-after-end `ValueError` is raised only when the caller supplied *both* ends —
otherwise the run reports nothing new, which is what it means.

**Not fixed, deliberately:** a week split across two windows (bootstrap ends
mid-week, so reflect's first window starts mid-week) is still cited from whichever
part was analysed first. Closing that would mean refusing weeks not wholly inside
the window, which would orphan every boundary week permanently. The snap removes
the case that recurs on every run; this one happens once per bootstrap.

---

## 11. Decisions & Open Questions

**Resolved**

- **`workout generate` is read-only.** It consumes active learnings (injected via
  `_build_system_prompt`) but authors none. This *stops* the existing
  `learning_updates` writes in `_generate_workouts_logic` — a behavioral change
  from today (ARCHITECTURE §3). Rationale: generation's observations are already
  better-authored elsewhere — tactical/recent ones by `adapt`, durable/cross-block
  ones by `analyze`. Durable memory stays limited to the deliberate paths
  (`data analyze`, `plan generate`). Revisit only if generation is empirically
  seen surfacing learnings the others miss.

  **AS BUILT — the decision stands, its justification narrowed.** `workout adapt`
  was subsequently made read-only w.r.t. learnings as well
  (DESIGN_evidence_based_confidence.md §2/§11), and `plan generate` never became a
  writer, so the "authored better elsewhere" argument now resolves to a single
  place: the analysis flow (`data bootstrap` / `data reflect`) is the *only*
  learnings writer. That is the stronger form of the same principle — a learning
  should be attributable to the weeks of evidence it was derived from, and only
  the flow that walks weekly summaries can cite them (§8 *AS BUILT*).

- **Prompt/science changes do *not* invalidate reuse (for now).** The evidence
  fingerprint hashes only activity/metric inputs, *not* the prompt or science
  files. So editing a prompt or `science/*.md` reuses a stale reconstruction
  until the underlying data changes; `--force` is the manual escape hatch.
  **Action:** add a code comment at the fingerprint computation making this
  limitation explicit, so the omission reads as deliberate, not forgotten. *As
  built the comment also names a second omission found later: a bare **baseline
  recomputation** that shifts a deviation without any in-window metric changing
  (baselines track the metrics, so in practice they move together).*

- **Cache storage → dedicated `analysis_cache` table.** See §5.1 for the schema,
  the `db.py` methods, and the one-row-per-horizon retention policy. Chosen
  because `data analyze` runs standalone (no plan to hang the cache off), it keeps
  everything in `trainmate.db`, stays typed/inspectable, and makes retention
  trivial. Rejected: columns on `macrocycles` (no home for standalone runs),
  inferred-cycle rows with a `kind` discriminator (two-concepts-one-table; no home
  for insights), a generic blob table (junk-drawer), on-disk JSON (breaks
  single-source-of-truth; no transactional consistency).

**Open**

- *(none)*

---

## 12. Goal completion is the date's verdict, not a stored state

Everything above assumes the review can see the goals the athlete has already
trained through. It could not, and the reason turned out to sit one level below
the review: the `objectives.status` column was being asked to carry two unrelated
facts at once.

### 12.1 One column, two questions

`status` held `active`, `completed`, or `archived`, and readers filtered on
`status = 'active'`. But the three values answer two different questions:

- **Is this goal still ahead of the athlete?** A date question. The answer is
  already written in `target_date`, and it changes on its own every midnight.
- **Did the athlete call this goal off?** A genuine state. Nothing but an explicit
  human act can tell you a goal was abandoned, and no amount of staring at dates
  will reveal it.

Storing the first question's answer in a column meant it had to be *maintained* —
and nothing maintained it. `completed` was only ever set by the athlete running
`goal edit --status completed` by hand, so the column drifted out of step with the
calendar the moment an event was raced.

### 12.2 What the drift actually cost

The two failure modes pull in opposite directions, which is what makes the
single-column design untenable rather than merely untidy.

**Leaving the goal `active` after its date passed.** `get_active_objective()` took
the earliest `active` goal with no date filter, so a raced event stayed "the next
goal" indefinitely. `plan generate` would then try to plan a window that ended in
the past and refuse — *"there is no window to plan in"* — and the only way out was
for the athlete to notice that a bookkeeping flag, not the plan, was the problem.
The app was reporting a data-entry chore as a planning failure.

**Marking the goal `completed`.** This fixed the refusal and broke the review.
`get_preceding_objectives` and `get_governing_macrocycle` both filtered on
`active`, so the moment a goal was marked completed its entire macrocycle vanished
from the prior-training review (§6.1) — and the mesocycle bands vanished from the
progress timeline with it, blanking the labels on the months of training behind the
athlete precisely when they had just finished the event and were most likely to
look. The athlete's reward for correct bookkeeping was a worse coach.

So the athlete was offered a choice between a command that refuses and a review
that forgets, and neither branch was the one they wanted.

### 12.3 The rule

**`status` records only whether the goal was called off — `active` or `archived`.
Completion is derived.**

```
goal_state(goal) =
    ARCHIVED   if status == 'archived'
    COMPLETED  if target_date < today      # the date decides, nothing else
    UPCOMING   otherwise
```

`db.objectives.goal_state()` is the single implementation, so no two surfaces can
disagree about what a goal's state is — the CLI's `goal list`, `status`, and the
web view all route through it. Nothing writes `completed` and nothing reads it off
a row; the derivation is re-evaluated on every read, which is what makes midnight
sufficient to advance it.

The accessors follow from the split, and each one now says which of the two
questions it is asking:

| Accessor | Means |
| --- | --- |
| `upcoming_objectives()` | not archived **and** date not yet passed — "the goals that matter" at every planning and picker site |
| `get_active_objective()` (no ID) | the next goal still ahead; date-filtered, so a raced event can no longer occupy the slot |
| `get_active_objective(id)` | any goal not archived, past or future — an ID is an explicit request for *that* goal |
| `get_preceding_objectives()` | goals before a date, **completed ones very much included** — they are the point of the lookup |
| `get_governing_macrocycle()` | the plan the current workouts implement (below) |

The ID form deliberately stopped filtering by date. When the athlete names a goal
they mean that goal, and a caller that needs a plannable window checks the window
itself and gives a better error than a missing row ever could.

### 12.4 The governing macrocycle needs a completed fallback

`get_governing_macrocycle()` picks the earliest goal-with-a-plan still ahead, and
its mesocycles label the progress timeline (DESIGN_progress_timeline.md §6.1). Once
completion became automatic, the day after an event there is nothing ahead with a
plan, and the labels would blank themselves overnight.

It therefore **falls back to the most recently completed goal's plan** when nothing
ahead has one. The months of training behind the athlete genuinely do belong to
that plan, and the timeline should keep saying so until a new plan exists to take
over. Advancing the labels used to be an accidental side effect of the athlete
marking a goal completed; it is now the date's job, and the fallback is what keeps
the handover from leaving a hole.

### 12.5 Migration and surface

Single-user app, so the migration is a one-off `UPDATE` in `db/base.py` rather than
a compatibility shim: any row still reading `completed` is rewritten to `active`,
after which the column's remaining meaning — called off or not — is true of every
row. The `objectives` schema comment records the narrowed vocabulary.

`goal edit --status` drops `completed` from its choices, since offering it would be
offering to write a state the app no longer reads. The remaining pair is described
in the athlete's terms — call the goal off, or reinstate it — and the help text says
outright that a goal completes on its own once its target date passes.

One nuance worth stating because the code does not make it obvious:
`progression.plan_gap()` filters `status != 'archived'` and *not* `upcoming`. It is
already selecting goals with `target_date > plan_end_date`, so the date question is
answered by that comparison; adding a second date filter would be redundant rather
than safer.

---

## 13. A response that parses is not yet a result

The 2026-08-18 model comparison ran the same bootstrap across fifteen models and
turned up a failure mode the code had no name for. `moonshotai/kimi-k3` returned
syntactically perfect JSON in which every *nested* key carried a `>` prefix:

```json
"inferred_macrocycle": {
  ">overall_focus": "Summer aerobic base building …",
  ">start_date": "2026-05-25"
}
```

Top-level keys were intact, so `_parse_json_content` succeeded and the response
walked the whole flow. Every `.get('overall_focus')` missed. The report printed
`Macrocycle Focus ( to ): N/A` over three nameless mesocycle blocks; all three
learning deltas had a `">op"` instead of an `"op"`, so `apply_learning_deltas` skipped
each one under its skip-malformed rule and said nothing; and the next command
greeted the user with *"No coach learnings yet. Run `data bootstrap`"* — the command
that had just run.

Three separate silences compounded there, and each is fixed where it lives.

**The reconstruction.** Parse success was standing in for a shape check that no
one was doing. A response is now judged by whether any part the app actually reads
came back with content: the summary, the physiological insights, and — on the long
horizon — a macrocycle focus and at least one named mesocycle. None of them
readable makes it a failed exchange, and it raises. That matters more than the
error message: the old path *cached* the emptiness against the evidence
fingerprint and moved the reflect watermark, so the window counted as read, the
next `data bootstrap` hit the repeat-run prompt, and confirming it returned the
same empty reconstruction from cache without another LLM call. Only `--force`
escaped. Failing before any write leaves the retry free — no cache, no watermark,
no bootstrap record.

Partial damage is not failure, though. One good part is a result worth keeping, so
a response with a real summary and a mangled macrocycle still saves — it just names
the parts that came back unreadable instead of letting them render as blank blocks.
Presence is the discriminator: an omitted key is the model declining to answer, a
populated one whose fields all miss is a shape mismatch.

**The deltas.** Skipping a malformed delta is still right (§8) — the alternative is
writing junk into the evidence basis. What was wrong is that the skip was invisible,
which made "the model authored nothing" and "the app understood nothing" look
identical. `apply_learning_deltas` now returns `{"applied", "skipped"}` and the
service reports a non-zero skip count. Note that `reinforce` against a re-cited week
counts as applied: landing no new week is the dedup working as designed, not a
failure to read.

**The nudge.** `_maybe_nudge_bootstrap` fires on "no active learnings", which is a
true statement in both a cold start and a bootstrap that seeded nothing — but only
the first is answered by running bootstrap. It now checks the `bootstrap`
`sync_state` key and, when the run has already happened, says so and points at
`--force`. Bootstrap itself closes the same gap from the other side: ending a run
with no active learnings is reported at the end of the run, where the user is
looking, rather than left to surface as a cold-start hint two commands later.

This is deliberately not a repair layer. Un-prefixing `">op"` would be guessing at
one model's quirk, and the next model will be wrong in some other way. The app's job
is to notice, refuse to persist an empty read, and say which part it could not use.

---

## 14. Calling a goal off stands its sessions down

§12 split the two questions `objectives.status` was conflating and left `archived`
meaning exactly one thing: the athlete called this goal off. What it did *not*
settle is what calling a goal off should do to the training already scheduled for
it.

### 14.1 Archiving hid the goal but left the calendar alone

Setting `status = 'archived'` hides the goal, its macrocycle and its mesocycles
from every planning reader — the objective filter in `get_governing_macrocycle`
and the `o.status = 'active'` join in the four date-keyed block lookups. The plan
rows themselves survive untouched, which is the point: `--status active`
reinstates the goal and everything about it comes back.

The sessions did not follow. `workouts` carries a `macrocycle_id` tag but no
foreign key to it, so archiving a goal left its remaining workouts live, dated,
and still on Google Calendar. The athlete called off a race and the calendar kept
telling them to run 32k on Sunday.

### 14.2 Why archive rather than delete

`goal rm` deletes the objective row, and `ON DELETE CASCADE` carries away every
macrocycle version, every mesocycle and the whole plan feedback log with it. That
is a strictly worse answer to the same problem, for two reasons.

The first is that this project already decided the question one level down. When
a regeneration supersedes a plan, the prior macrocycle is **not** deleted — it is
marked `superseded` and kept so `plan rollback` can restore it (`macrocycles.status`,
`superseded_at`; DESIGN_plan_rollback.md). Destroying every one of those versions
because the goal above them went away would undo that decision from the top.

The second is that deletion does not even solve the calendar problem. The
sessions are tagged with a macrocycle, not owned by it, so a cascade leaves them
exactly as live as archiving did — only now nothing remains that could explain
what they were for. Deletion strands the sessions *and* burns the history.

So archiving is the normal path and it stands the sessions down; `goal rm` stays
as the escape hatch for a goal entered by mistake.

### 14.3 The sweep is scoped by plan version, not by date

`archive_future_workouts` already existed for regeneration and `plan rollback`,
and it takes every live workout from a date onward. That floor is right —
a called-off race does not un-train the months already behind the athlete — but
the date alone is the wrong *filter*: two goals' plans routinely have sessions in
the same week, and a horizon long enough to cross from one goal's last block into
the next is a case the generator explicitly supports.

So the sweep takes an optional set of macrocycle IDs and archives only sessions
tagged with them. Every version the goal owns is passed, not just the active one:
a superseded version can still hold live rows.

Untagged rows (`macrocycle_id IS NULL`, predating the tag) match no version and
are therefore never swept. They are counted and reported instead. Guessing which
goal a tagless session belonged to would be a coin flip, and silently sweeping it
would be the same destructive move this section exists to avoid.

### 14.4 Reinstating asks first

Archival is not a prompt — it is reversible, and the summary line says what it
took. Reinstating *is* a prompt, because it is the direction that can surprise:
a goal picked back up months later would otherwise silently re-push sessions from
a plan that no longer suits the athlete. The restore is floored at today by
`restore_workout_batch`, so a late reinstate recovers only what is still ahead;
declining leaves the batch archived and `workout batches` still lists it.

### 14.5 `goal rm` says what it is about to take

Deleting a goal remains possible and remains a cascade. It now prints the
inventory first — plan versions, blocks, feedback notes, and the count of
upcoming sessions it would strand — names `goal edit --status archived` as the
reversible alternative, and asks. `-y` skips the prompt for scripted use, as on
`goal wipe`.

---

## 15. Out of Scope

- Backward evaluation in `workout adapt`.
- Per-learning evidence provenance.
- Pulling Garmin data directly (separate TODO).
- Workout swapping (separate TODO).
