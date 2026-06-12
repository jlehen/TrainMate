# Design: Evidence-Based Learning Confidence

**Status:** Draft · **Date:** 2026-06-12 · **Branch:** `evidence-based-confidence` (proposed)

This picks up the item both DESIGN_backward_evaluation.md §8 ("Edges") and §12
("Out of Scope") explicitly deferred as YAGNI: **per-learning evidence
provenance.** It replaces LLM-assigned confidence with confidence the *app*
computes from a per-learning record of which weeks actually backed each
observation, and adds a downward force (contradicting evidence) the current model
lacks.

---

## 1. Motivation

A coach learning carries a `confidence` (`tentative | moderate | established`)
that weights how much the planner trusts it. Today that level is **whatever the
LLM writes** in a delta, and it can only ever move **up or dormant**:

- **Not anchored to evidence.** "established" is an LLM vibe, not a fact about how
  much corroboration exists. The model can jump a one-week observation straight to
  "established," or lag behind a well-supported one. The label is not auditable.
- **One-directional.** Decay is *soft dormancy by recency* (a learning fades when
  unreinforced past its budget — see `db/learnings.py::learning_is_dormant`).
  Training that **contradicts** a learning has no effect: the learning just sits
  there until it quietly fades. There is no demotion.

**What is already handled (and what this is not).** The acute "re-running inflates
confidence" bug from DESIGN_backward_evaluation §8 is *already* contained by two
shipped mechanisms:

1. the **reflect watermark** (`data reflect` only sees weeks after the last run,
   so overlapping evidence is not re-counted — see [reflect/bootstrap split]); and
2. the **window-level evidence fingerprint** + `suppress_reinforcement`
   (a forced re-read of unchanged evidence drops the reinforce ratchet).

So this is **not** a rescue. It is a correctness/honesty upgrade: make the
confidence *label* mean something concrete, and give contradiction a voice. The
fingerprint's all-or-nothing guard also still leaks on a *partial* window change
(§8 "Edges": add one session → fingerprint shifts → the whole window's
reinforcement re-fires, including for learnings whose real support is older weeks).
A per-learning basis closes that structurally.

---

## 2. Goals / Non-Goals

**Goals**
- Confidence is a **deterministic function of accumulated evidence**, computed by
  the app, not asserted by the LLM.
- That evidence is **de-duplicated**, so re-reading a week already counted is a
  structural no-op — pseudo-replication becomes impossible regardless of window
  shape (subsumes the §8 integrity invariant).
- **Two-sided pressure** from **both** directions a learning can weaken:
  *contradicting* training (you did the opposite) and *staleness* (it has gone
  unused past its recency budget). Both lower confidence.
- A demotion is **proposed, never silently applied** by default — the human
  confirms it (the agreed "demote gradually" + "flag, don't act" mix). An opt-in
  `--auto` path applies staleness demotions unattended (§7).

**Non-Goals**
- Per-*session* provenance. Evidence is keyed on **weeks** (see §4) — the unit the
  LLM is actually shown.
- Silent auto-erasure. Demotions are human-confirmed by default; `--auto` is an
  explicit opt-out for unattended runs, and only for *staleness* (§7).
- Replacing recency dormancy. Dormancy stays as the "hide from prompts" mechanism;
  this only *adds* a demotion proposal when a learning crosses into it (§8).
- A confidence model for `workout adapt` (it does not write durable learnings).

---

## 3. Core Concept

Each learning owns an **evidence basis**: the set of distinct weeks that have
backed it, each tagged supporting or contradicting. Confidence is then a pure
function of that basis:

```
net_support = (# distinct supporting weeks) − (# distinct contradicting weeks)

net_support ≥ established_min (default 5)  → established
net_support ≥ moderate_min    (default 3)  → moderate
net_support ≥ 1                            → tentative
net_support ≤ 0                            → (proposed retirement)
```

The two cut points are **configurable in `config.yaml`** (the project uses YAML,
not env vars — see [garmin direct-pull design]), mirroring how
`LEARNING_STALENESS_DAYS` already parameterizes the dormancy budgets:

```yaml
learning_confidence_thresholds:   # distinct net supporting weeks to reach each level
  moderate: 3
  established: 5
```

A loader reads these with the defaults above as fallback (tentative is always ≥1,
retirement always ≤0 — not knobs). Tuning the map re-levels learnings on the next
recompute without a migration.

The LLM no longer sets confidence. It does the one thing it is positioned to do —
**attribute observations to the weeks of training it is looking at** — and the app
does the counting and the leveling. Re-citing a week already in the basis changes
nothing, so confidence cannot be inflated by re-running, by `--force`, or by an
overlapping window.

**Upgrades auto-apply; downgrades are proposed.** Crossing a threshold *upward* is
earned and safe, so the app applies it immediately. Crossing *downward* (from
contradiction) erases hard-won trust on what might be one odd week, so the app does
**not** apply it — it records a `proposed_confidence` and surfaces it in `status`
for the human to accept or dismiss (§7).

---

## 4. Why Weeks, Not Sessions

The analysis prompt (`CoachService._run_workout_analysis`) feeds the LLM **weekly
aggregate summaries** keyed by `week_commencing` (totals, zone seconds, RHR/HRV,
highlights) — never a list of individual workouts with stable IDs. So the only
evidence anchor the model can actually *point at* is the week.

This is a feature, not a compromise:

- **Weeks dedupe trivially** — one `YYYY-MM-DD` (Monday) string per bucket.
- **The gate thresholds become span-free.** The original sketch needed "≥3
  sessions *over ≥2 weeks*" to stop five sessions in one week from minting
  "established." Counting **distinct weeks** encodes the span automatically: "≥3
  distinct weeks" *is* "spread across ≥3 calendar weeks." No separate span check.
- **No prompt restructure.** The weeks are already in front of the model.

Going to session granularity would mean reshaping what the LLM sees and managing
stable session IDs, for marginal benefit. Deferred (§10).

---

## 5. Data Model

A new table; the existing `coach_learnings` row keeps `confidence` as the
**app-maintained** current level (no longer LLM-written) and gains a nullable
`proposed_confidence`.

```
learning_evidence
| Column          | Type       | Notes                                              |
|-----------------|------------|----------------------------------------------------|
| id              | INTEGER PK |                                                    |
| learning_id     | INTEGER    | FK → coach_learnings.id (ON DELETE CASCADE)        |
| week_commencing | TEXT       | YYYY-MM-DD (Monday) — the evidence anchor          |
| polarity        | INTEGER    | +1 supporting · −1 contradicting                   |
| source          | TEXT       | 'reflect' | 'bootstrap' | 'plan' | 'migration'     |
| created_at      | TEXT       | ISO timestamp                                      |
UNIQUE(learning_id, week_commencing, polarity)
```

`UNIQUE(learning_id, week_commencing, polarity)` is the dedup guarantee: re-citing
a counted (week, polarity) pair is an `INSERT OR IGNORE` no-op. A week may appear
once as +1 and once as −1 (genuine flip over time); `net_support` nets them.

`coach_learnings` changes:
- `confidence` — still present, but now **derived/written by the app** from the
  basis. Never set from an LLM delta.
- `proposed_confidence TEXT NULL` — a pending, human-confirmable **downgrade**
  (`NULL` when none pending).

`confidence` is denormalized (recomputable from the basis) so existing readers
(`status`, prompt rendering) keep working with no join. The app rewrites it inside
the same transaction that mutates the basis.

---

## 6. Delta Protocol Changes

`learning_updates` deltas (LEARNING_UPDATES_FIELD in `coach/engine.py`) change so
the LLM attributes evidence instead of asserting confidence:

| op           | shape                                              | effect |
|--------------|----------------------------------------------------|--------|
| `add`        | `{text, sports?, evidence:[weeks]}`                | create learning; seed supporting basis from `evidence`; confidence derived |
| `revise`     | `{id, text?, sports?, evidence?:[weeks]}`          | content edit; if `evidence` given, also add supporting weeks |
| `reinforce`  | `{id, evidence:[weeks]}`                            | add supporting weeks (no wording change). **No `evidence` ⇒ no-op** |
| `contradict` | `{id, evidence:[weeks]}`                            | **new.** add contradicting weeks; may trigger a *proposed* demotion (§7) |
| `retire`     | `{id}`                                              | unchanged — explicit LLM retirement |

Removed: the `confidence` field on every op, and the prose telling the model to
"raise it as evidence accumulates." Confidence is no longer the model's to set.

**Validation (consistent with the existing "skip malformed" philosophy in
`apply_learning_deltas`).** The app validates each cited week against the actual
set of `week_commencing` values in the analysis window; weeks outside it (LLM
hallucinations, or weeks from a window not under analysis) are dropped silently,
exactly as malformed deltas are skipped today. A delta whose evidence fully drops
out becomes a no-op.

**All learning-writing flows use this protocol.** `LEARNING_UPDATES_FIELD` is
shared, so `data reflect`/`data bootstrap` *and* `plan generate` (also a learnings
writer — DESIGN_backward_evaluation §4) emit evidence-cited deltas. `plan generate`
cites weeks from the reconstruction window it already analyzes; weeks are validated
against that window the same way (§ Validation). Keeping one protocol across all
writers avoids a second, un-anchored confidence path.

**Recency now follows evidence.** `last_reinforced_at` (the dormancy clock) is
refreshed to the run time only when a *new* supporting week actually lands in the
basis — not merely because a delta was emitted. This is what makes "looking at it
keeps it fresh forever" impossible, and it falls out for free from dedup.

---

## 7. Demotion: Propose, Don't Apply

Two independent signals weaken a learning, and **both** feed one human-confirmed
demotion channel:

1. **Contradiction.** `contradict` rows accumulate in the basis (deduped like
   supporting ones), lowering `net_support` (§3).
2. **Staleness.** A learning that crosses its recency budget into dormancy (§8) is
   itself evidence of weakening — long silence should cost confidence, not just
   hide the record. Crossing the budget proposes a **one-level** step down.

After applying a run's deltas (and when evaluating dormancy), the app derives the
target level per touched learning:

- **Derived level ≥ stored level** → apply immediately (upgrade or unchanged).
  Upward moves are earned and safe.
- **Derived level < stored level** → do **not** change `confidence`. Set
  `proposed_confidence` to the derived level (or, if `net_support ≤ 0` or a
  tentative learning ages out, the sentinel proposing **retirement**). The learning
  keeps operating at its current level until the human rules.

Staleness demotion is **gradual and re-arming**: established → (180d silence) →
propose moderate → (60d) → propose tentative → (21d) → propose retire. Each
accepted step adopts the lower level's shorter budget, so an untouched learning
walks down to retirement over time rather than vanishing in one jump.

`status` surfaces a pending proposal, e.g.:

```
[4|cycling|moderate] Responds well to back-to-back hard days.
   ⚠ proposed demotion → tentative  (2 contradicting weeks since 2026-05-04)
   confirm with:  trainmate data learning demote 4   |   keep with:  ... keep 4
```

Two human actions, one new CLI verb group `data learning`:
- **`data learning demote <id>`** — accept: write `confidence = proposed_confidence`
  (or retire on the retirement sentinel), clear `proposed_confidence`.
- **`data learning keep <id>`** — dismiss + **affirm**: clear `proposed_confidence`
  **and** durably settle the trigger so it is not re-raised every run — for a
  *contradiction* proposal, neutralize the −1 rows; for a *staleness* proposal,
  refresh `last_reinforced_at` (the human affirming counts as reinforcement). Either
  way the human has overruled the data for now.

**`--auto` (unattended runs).** `plan generate` already carries an `--auto` flag
for non-interactive use (plans.py — skips prompts, see [reflect/bootstrap split]
nudge). The learning-writing commands `data reflect`/`data bootstrap` do **not**
have one today and would **gain** it, with the same "no prompts" meaning. Under
`--auto` there is no human to confirm, so **staleness** demotions are **applied
directly** instead of queued — this is exactly the case the user opted into:
time-driven, low-stakes, and pointless to queue when nobody will review it. **Contradiction** demotions are
*not* auto-applied even under `--auto`; they are higher-stakes (the LLM read recent
training as disconfirming a real pattern) and stay queued as proposals for the next
interactive review. This keeps the strong caution on contradiction while letting
staleness decay on its own when run from cron.

---

## 8. Interaction With Existing Mechanisms

**The window fingerprint reverts to a pure optimization.** Today the fingerprint
plays two roles (DESIGN_backward_evaluation §5): (1) cheap-skip/reuse, and (2) the
§8 *integrity invariant* via `suppress_reinforcement`. The per-learning basis now
**owns integrity** — re-citing counted weeks is a structural no-op, on any window
shape, which is strictly stronger than the all-or-nothing fingerprint guard. So:

- Role 1 (reuse / skip the LLM call on unchanged evidence) **stays** — still a
  worthwhile cost saving.
- Role 2 is **retired.** `suppress_reinforcement` (in `apply_learning_deltas` and
  threaded through `CoachService`) is removed; the basis subsumes it. Keeping two
  mechanisms for one property invites drift. This deletes the `evidence_unchanged`
  plumbing in `_run_workout_analysis` and simplifies `_apply_learning_updates`.

**Dormancy gains a demotion role.** Recency dormancy (`learning_is_dormant`) still
hides stale learnings from prompts, but crossing the budget now *also* proposes a
one-level staleness demotion (§7) — so silence costs confidence, not just
visibility. The budget itself, the revive-on-reinforce behavior, and the
`last_reinforced_at` clock are unchanged (the clock now advances on new supporting
weeks, per §6). Contradiction (you did the opposite) and staleness (you did
nothing) are distinct triggers feeding the same proposal channel.

**Bootstrap legitimately earns high confidence.** A cold-start `data bootstrap`
over a long backlog may cite many distinct weeks for one learning and derive
`established` in a single pass — correct, because the weeks genuinely exist. A
later re-bootstrap re-cites the same weeks → deduped → no change.

---

## 9. Migration

Existing learnings have a `confidence` but no basis; we cannot reconstruct which
weeks supported them. **Grandfather** rather than reset, to avoid silently
demoting everything on upgrade:

- Seed a synthetic supporting basis sized to *sustain each learning's current
  level* (`established` → 5 rows, `moderate` → 3, `tentative` → 1), all dated at
  the learning's `created_at` week, `source = 'migration'`, `polarity = +1`.
- Real evidence then accrues on top; the synthetic rows simply hold the floor.

The phantom weeks are deliberately the *same* synthetic date, so they count as a
single distinct week going forward? — **No:** that would collapse the floor. Use
distinct synthetic Monday dates stepping back from `created_at` (one per needed
week) so `# distinct supporting weeks` matches the grandfathered level. `source =
'migration'` keeps them auditable and lets a future pass distinguish real from
seeded evidence.

Schema is additive (new table + nullable column), consistent with the in-place
`ALTER TABLE ... ADD COLUMN` idiom already in `db/base.py`.

---

## 10. Decisions & Open Questions

**Resolved**
- **Week granularity** over session (§4) — fits the prompt, encodes span for free.
- **App computes confidence; LLM only attributes evidence** (§3, §6). The
  `confidence` field is removed from the delta protocol.
- **Upgrades auto, downgrades proposed** (§7) — the agreed "propose gradual demote,
  human decides" mix.
- **Staleness also demotes**, not just dormancy-hides (§7, §8) — a learning unused
  past its budget proposes a one-level, re-arming step down. By default it is a
  proposal; under **`--auto`** (unattended runs) staleness demotions apply directly,
  while contradiction demotions still wait for interactive review.
- **`suppress_reinforcement` removed**, basis owns integrity (§8).
- **Grandfather existing learnings** with synthetic basis (§9).

- **Thresholds are config-driven** (§3) — `learning_confidence_thresholds` in
  `config.yaml` (`moderate`/`established`), defaulting to 3 / 5; tentative ≥1 and
  retirement ≤0 are fixed. Re-levels on recompute, no migration.
- **`keep` drops the −1 (contradicting) rows** (§7) — chosen over pinning an
  affirm-date. Simpler; we forgo the (speculative) "user overruled N times" signal.
- **`plan generate` cites evidence weeks too** (§6) — one shared, anchored protocol
  across all learnings writers; no separate un-anchored confidence path.

**Open**

---

## 11. Out of Scope
- Per-session evidence provenance.
- Auto-applied *contradiction* demotion/retirement (always human-confirmed; only
  *staleness* demotions auto-apply, and only under `--auto` — §7).
- Confidence in `workout adapt` / `workout generate` (read-only consumers).
- Reworking recency dormancy.
