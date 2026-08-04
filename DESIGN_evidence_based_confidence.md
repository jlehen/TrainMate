# Design: Evidence-Based Learning Confidence

**Status:** Implemented (2026-06-12) · **Branch:** `evident-based-confidence-claude`

> **Rev. 2 (2026-08-04) — reconciliation with the code as built.** The model itself
> (evidence basis, dedup, upgrade-auto / downgrade-propose, two-sided pressure, `--auto`,
> the grandfather migration) shipped exactly as designed. What drifted afterwards was the
> **curation UX**: a `learnings` CLI verb group was added, and the per-learning proposal
> line moved out of `status` into it (§7 *AS BUILT rev 2*, §10). Also corrected here: the
> no-evidence case of the §3 mapping, the `source` enum in §5, the shape and default of
> the confirm prompt (§7), what `--auto` does at the bottom rung (§2, §7), and stale
> symbol/module paths (§3, §4, §6). Superseded text is struck through, not deleted.

**Implementation notes — decisions taken where the design met the codebase:**
- **`plan generate` stays a non-writer.** The design (§6) assumes it authors
  learnings, but in this codebase it never did (only the analysis flow and `adapt`
  wrote). Rather than build a new learnings-writing path into it, the evidence model
  lives in the analysis flow (`data bootstrap`/`data reflect`), the only flow that
  shows the LLM weekly summaries. Instead, when there are no learnings yet,
  `plan generate` **and** `status` nudge the user to run `data bootstrap`.
- **`adapt` is read-only.** It has no weekly evidence to cite, so it no longer
  authors learnings at all (it consumes them as context) — matching this doc's
  "adapt does not write durable learnings" premise (§2/§11). The old
  `_apply_learning_updates(decision)` call and the learning-writing instructions in
  `_adapt_logic`'s prompt are removed.
- ~~**No new CLI verbs.**~~ **Superseded — see §7 *AS BUILT (rev 2)*.** The
  propose/confirm flow (§7) first shipped as **interactive prompts** at the end of
  `data bootstrap`/`data reflect`, with `--auto` skipping them. A later change added an
  out-of-band `learnings` command family, so `demote`/`keep` are now reachable both from
  the run-end prompt and as standalone verbs; `status` was slimmed to a one-line count at
  the same time.

Original draft preamble follows.

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
   *(Correction: `suppress_reinforcement` was designed but never actually shipped — see
   DESIGN_backward_evaluation.md §8 *AS BUILT*. Only the fingerprint existed, so the
   "already contained" claim rested on the fingerprint alone.)*

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
  explicit opt-out for unattended runs, and only for *staleness* (§7). Note that the
  opted-into `--auto` ladder *does* end in a hard delete at its bottom rung — §7 spells
  out why that is the ladder finishing, not erasure behind the user's back.
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
net_support ≤ 0 *and* ≥1 contradicting wk  → (proposed retirement)
net_support ≤ 0 with an empty basis        → tentative (the floor)
```

**Retirement needs an actual contradiction.** The `≤ 0` rung fires only when
contradicting weeks drove it there. A learning with no basis at all (net 0, nothing
against it) rests at the tentative floor instead — otherwise every freshly added,
not-yet-cited learning would be proposed for retirement on its first recompute.

The two cut points are **configurable in `config.yaml`** (the project uses YAML,
not env vars — see [garmin direct-pull design]), mirroring how
`config.learning_staleness_days` already parameterizes the dormancy budgets:

```yaml
learning_confidence_thresholds:   # distinct net supporting weeks to reach each level
  moderate: 3
  established: 5
```

A loader reads these with the defaults above as fallback (tentative is always ≥1,
retirement always ≤0 — not knobs); `config_template.yaml` carries the commented block
next to `learning_staleness_days`. Tuning the map re-levels learnings on the next
recompute without a migration — `db.recompute_all_confidence()` is the entry point that
re-derives every learning from its basis on demand.

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

The analysis prompt (`CoachService._run_workout_analysis`, now in
`coach/service/analysis.py`) feeds the LLM **weekly
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
| source          | TEXT       | 'reflect' | 'bootstrap' | 'manual' | 'migration'   |
| created_at      | TEXT       | ISO timestamp                                      |
UNIQUE(learning_id, week_commencing, polarity)
```

`UNIQUE(learning_id, week_commencing, polarity)` is the dedup guarantee: re-citing
a counted (week, polarity) pair is an `INSERT OR IGNORE` no-op. A week may appear
once as +1 and once as −1 (genuine flip over time); `net_support` nets them.

**AS BUILT — the `source` values.** `'plan'` is never written: `plan generate` stayed a
non-writer (§6 *AS BUILT*), so the value survives only as a vestige in the schema
comment. `'manual'` was added instead, for `db.add_learning(text, sports, confidence)` —
a hand-added learning seeds a synthetic supporting basis sized to sustain the confidence
it was created with, so the first recompute does not demote it for lack of evidence. It
is the per-row analogue of the §9 grandfather migration.

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

`learning_updates` deltas (`LEARNING_UPDATES_FIELD`, now in `coach/engine/__init__.py`
after the engine became a package) change so the LLM attributes evidence instead of
asserting confidence:

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

> **AS BUILT (supersedes the `plan generate` half above).** In the implemented
> codebase `plan generate` was **never** a learnings writer (only the analysis flow
> and `adapt` were), so it was left a non-writer rather than growing a new
> learnings-writing path. The shared evidence-cited protocol is emitted **only** by
> `data bootstrap`/`data reflect` (the flow that shows the LLM weekly summaries).
> `adapt` was made fully read-only. To cover the cold start, `plan generate` and
> `status` instead **nudge** the user to run `data bootstrap`.

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

The three human actions and their effects are:

- **demote** → `db.demote_learning(id)`: write `confidence = proposed_confidence`
  (or retire on the retirement sentinel), clear `proposed_confidence`, re-arm the clock.
- **keep** → `db.keep_learning(id)`: dismiss + **affirm** — for a *contradiction*
  proposal, neutralize the −1 rows; for a *staleness* proposal, refresh
  `last_reinforced_at`. Either way clear `proposed_confidence`.
- **skip** → leave it pending; re-raised on the next run.

> **AS BUILT (rev 1) — resolved at the end of a run, not by new CLI verbs.** The three
> actions are offered as an **interactive prompt at the end of `data bootstrap`/`data
> reflect`**, one block per pending proposal. It is not the `y`/`N`/`s` `input()` idiom
> the draft assumed: it uses the shared `cli.prompt.choose` selection menu, with
> **`skip` as the default answer** — the safest of the three, since an unattended Enter
> then changes nothing. `--auto` skips the prompt entirely (staleness applied directly,
> contradiction left queued).

> **AS BUILT (rev 2) — a `learnings` verb group was added later, and `status` slimmed.**
> Out-of-band curation turned out to be wanted, so the run-end prompt gained standalone
> siblings: `learnings list | show | edit | rm | demote | keep | wipe`. `demote`/`keep`
> call the same `db.demote_learning`/`db.keep_learning`, so a proposal can be resolved
> either at the end of a run or at any later moment. `learnings show <id>` additionally
> renders the per-week evidence basis (supporting/contradicting, each with its `source`)
> — the audit view §1 asks for but never specified.
>
> At the same time the per-learning proposal line moved **out of** `status`. `status` now
> prints only a one-line roll-up:
>
> ```
> Coach Learnings:
>   7 active, 2 dormant, 1 pending demotion — see `learnings list`
> ```
>
> and the full line lives in `learnings list` / `learnings show`:
>
> ```
> [4|cycling|moderate] Responds well to back-to-back hard days.
>    ⚠ proposed demotion → tentative (confirm with `learnings demote`/`learnings keep`)
> ```
>
> The web front-end exposes the **read** half only — `GET /api/learnings` and
> `GET /api/learnings/<id>/evidence`. The dashboard is read-only by design, so there are
> no `demote`/`keep` HTTP endpoints; resolving a proposal is a CLI (or run-end) action.

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

**The bottom rung of the `--auto` ladder erases the row.** Stating plainly what "walks
down to retirement" means unattended: when a *tentative* learning goes dormant under
`--auto`, the step down lands on the retirement sentinel, and there is no proposal queue
to park it in — so the learning is `DELETE`d from `coach_learnings` outright (its basis
cascades). This is the intended end of the ladder, not silent auto-erasure in the §2
sense: it takes a full budget of silence at *every* level to get there (180d → 60d →
21d, each step re-arming the clock), and it only ever happens on a run the user
explicitly asked to be unattended. Interactively the same learning stops one rung
earlier, as a `retire` proposal waiting on a human.

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
  *(AS BUILT: role 2 never shipped in the first place — DESIGN_backward_evaluation.md §8
  *AS BUILT* — so this was a design retirement, not a code deletion. `evidence_unchanged`
  survives in `_run_workout_analysis`, but purely as the reuse/cache-hit test of role 1;
  `apply_learning_deltas(deltas, available_weeks=None, source=…)` has no suppression
  parameter.)*

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
- ~~**`plan generate` cites evidence weeks too** (§6)~~ — **superseded at
  implementation:** `plan generate` was never a learnings writer in this codebase, so
  it stayed a non-writer; the evidence-cited protocol is emitted only by
  `data bootstrap`/`data reflect`, with a cold-start nudge from `plan generate`/`status`
  (see §6 *AS BUILT*).
- **Propose/confirm via interactive prompts** (§7 *AS BUILT rev 1*) — resolved at the end
  of `data bootstrap`/`data reflect` via a `prompt.choose` menu defaulting to `skip`;
  `--auto` skips it.
- ~~**No `data learning` verb group was added.**~~ — **superseded (§7 *AS BUILT rev 2*):**
  a `learnings` group (`list`/`show`/`edit`/`rm`/`demote`/`keep`/`wipe`) was added
  afterwards for out-of-band curation, and the per-learning proposal line moved there out
  of `status`, which kept only a one-line count. The web surface stayed read-only.
- **Under `--auto`, the last staleness step deletes the learning** (§7) — the bottom of
  the "walks down to retirement" ladder, reached only after a full silence budget at
  every level, and only on a run the user marked unattended.

**Open**

---

## 11. Out of Scope
- Per-session evidence provenance.
- Auto-applied *contradiction* demotion/retirement (always human-confirmed; only
  *staleness* demotions auto-apply, and only under `--auto` — §7).
- Confidence in `workout adapt` / `workout generate` (read-only consumers).
- Reworking recency dormancy.
