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

**Option A chosen for §6/§7 (planned-vs-inferred).** During implementation the
§7 *auto-write into the `feedback` field* collided with the data model:
`db.save_macrocycle()` deletes-and-recreates the macrocycle (and its mesocycles)
on every regeneration, so feedback never persists across a replan — there is no
stable field to overwrite. Resolution (**Option A**): the planned-vs-actual review
is built read-only by `CoachService._build_prior_training_context()` (anchored on
the prior plan's elapsed mesocycle windows, augmented with the cached
reconstruction's insights — reused without a new LLM call), **injected into the
strategy prompt and displayed**, and writes to *no* `feedback` field. The §7
auto-write / 4-step-ordering subsection below is therefore **superseded** and
retained only for rationale. `plan feedback --edit` remains the human's path to
that field.

This document captures the design for feeding *backward-looking* analysis of
past training into *forward-looking* decisions (planning and workout
generation). It supersedes the relevant TODO items:

- "Provide previous macro- and meso-cycles when adapting or generating workouts?"
- "`data analyze` or `data adapt` saves to memory?"
- "Automatic data analysis: over a longer period when planning macrocycles and
  mesocycles; over a shorter period when planning microcycles and workouts."

---

## 1. Motivation

`data analyze` already reverse-engineers past training into a rich result —
`{macrocycle_summary, inferred_macrocycle, inferred_mesocycles[],
physiological_insights[], learning_updates[]}` (see ARCHITECTURE §3, §10). Today
only `learning_updates` survives; the reconstruction and insights are printed
and discarded.

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

**Non-Goals**
- No backward evaluation in `workout adapt` (see §4) — it runs daily; the cost
  is not justified.
- No per-learning evidence provenance in v1 (see §7, "Edges") — deferred as
  YAGNI.
- The human-facing narrative `macrocycle_summary` is *not* fed to the LLM (it is
  useful to read, but not decision-relevant for the model).

---

## 3. Core Concept

A **backward evaluation** is an LLM pass over past completed activities + metrics
that reconstructs/assesses what actually happened. It serves two distinct jobs
that must not be conflated:

- **Durable memory formation** — distilling general, cross-block,
  decaying observations into `coach_learnings` ("responds badly to consecutive
  hard days"). Slow-moving. Owned by `data analyze`.
- **Situational assessment** — a point-in-time judgement about a *specific* past
  period ("*this* base block did not build base"), consumed immediately by the
  plan it informs and written to that block's `feedback`. Owned by
  `plan generate`.

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

**Horizon dictates the product, not just the window size.**
- Long horizon → cycles + insights + planned-vs-inferred diff.
- Short horizon → physiological / recent-response read only. *Cycle
  reconstruction is meaningless below the long horizon* — you cannot infer a
  periodization structure from four weeks.

---

## 5. The Reconstruction Artifact (persist + reuse)

The reconstruction is a **cached artifact keyed by an evidence fingerprint**, not
a throwaway. This is what lets `plan generate` reuse `data analyze`'s work
instead of paying for a second backward pass (see §9).

- **Evidence fingerprint** — a hash of the *inputs* to the analysis: the set of
  completed-activity IDs + metrics within the window (plus the window itself).
  This follows the existing `goals_hash` / `lifeevents_hash` / `config_hash`
  reuse idiom (ARCHITECTURE §10).
- **Cache key** = (window/horizon, evidence fingerprint). Hashing the concrete
  activity-id set — not merely the date range — narrows the overlapping-window
  edge (see §7).
- `data analyze` produces/refreshes the artifact and curates learnings.
- `plan generate` consumes the matching artifact when the fingerprint is current,
  recomputing only if stale or absent.

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
- `wipe_analysis_cache()` — for `data wipe` symmetry.

Reuse decision (in `CoachService`): compute the current fingerprint, call
`get_analysis_cache(horizon)`; reuse when fingerprints match and `--force` is
absent, else recompute and `save_analysis_cache(...)`.

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

---

## 7. Feedback Auto-Write

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

Adding writers (`plan generate` now contributes learnings too) and allowing
curiosity re-runs surfaces a soundness bug in `reinforce`.

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

**Critical guardrail.** `--force` bypasses the *reuse optimization*, **never the
integrity invariant** (§8). Forcing a re-run over unchanged data still suppresses
spurious `reinforce`s; it only lets you *see* a fresh reconstruction and pick up
genuinely new `add`/`retire`. `--force` is never a license to double-count.

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
  `plan generate` now auto-writes its *own* feedback (§7).
- The **persisted artifact (§5) removes the redundant second pass**: when
  `analyze` has already reconstructed the current evidence, `plan generate`
  *reuses* that artifact for its diff instead of recomputing.
- The fingerprint invariant (§8) makes back-to-back runs harmless regardless.

`data analyze` earns a separate command because you sometimes look backward
*without* wanting a new plan — to curate memory on its own cadence, or simply out
of curiosity (especially early on). That is `--inspect-only`'s reason to exist.

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

- **Prompt/science changes do *not* invalidate reuse (for now).** The evidence
  fingerprint hashes only activity/metric inputs, *not* the prompt or science
  files. So editing a prompt or `science/*.txt` reuses a stale reconstruction
  until the underlying data changes; `--force` is the manual escape hatch.
  **Action:** add a code comment at the fingerprint computation making this
  limitation explicit, so the omission reads as deliberate, not forgotten.

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

## 12. Out of Scope

- Backward evaluation in `workout adapt`.
- Per-learning evidence provenance.
- Pulling Garmin data directly (separate TODO).
- Workout swapping (separate TODO).
