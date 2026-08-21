# Design: What in the athlete profile makes a plan stale

**Status:** Implemented · **Date:** 2026-08-21 · **Branch:** main

## 1. Problem

`config_changed()` flagged the active periodization stale whenever *any* `user_profile`
field changed. The rule was a one-line denylist in `config.plan_profile()` — everything
except the threshold anchors (`max_hr`/`lthr`/`ftp`, which route to the tolerance axis
instead) counted as plan-shaping.

Two things were wrong with that.

**It over-triggered.** Correcting a typo in `name` proposed regenerating the whole
periodization. `name` reaches the prompt, but it is a label: no periodization has ever
come out differently because the athlete is called Sam rather than Alex. Same for the
top-level `equipment` list, whose realistic edits are additive ("bought trail shoes") and
land at session level.

**It said nothing.** The reason string was the flat `"athlete profile changed"`. The
threshold axis sitting immediately beside it already produced `"ftp changed 250 → 270
(+8.0%)"` — specific enough to judge in a second — while the profile axis left the athlete
to diff `config.yaml` by hand to find out what it meant.

The same argument was already made and accepted for constraints
(`DESIGN_constraints.md` §7): *"Tactical directives ('no run Thursday') must not flag the
plan stale — hashing every constraint would make each quick capture trip the 'inputs
changed' regen proposal."* The profile fingerprint simply never got the same treatment.

## 2. The test

> Would the **periodization** have come out structurally different if this field had held
> a different value at generation time?

Not "would the LLM have noticed it" — every profile field reaches every prompt through the
one `_format_athlete_profile()` block, so prompt presence proves nothing. The question is
whether the block structure, phase ordering, or volume ramp depends on it.

## 3. The partition

| Field | Plan-shaping | Why |
|---|---|---|
| `name` | **no** | A label. Nothing downstream branches on it. |
| `equipment` | **no** | Edits are additive and session-level; per-day kit in `weekly_schedule` is what actually gates a session. |
| `birth_year` | yes | Age drives recovery capacity and intensity distribution. Changes only as a *correction* — you do not age into a new birth year — so it fires rarely, and when it does the plan was built on a materially wrong age. |
| `weekly_target_hours` | yes | Volume is the spine of the periodization. |
| `sport_preferences` | yes | The modality palette. Adding or dropping a sport changes the mesocycle mix wholesale. |
| `chronic_injuries` | yes | Free text, but structural in content ("no court sports"). The one field where *under*-triggering has a physical cost. |
| `preferences` | yes | Mixed bag — see §4. |
| `weekly_schedule` | split | See §4. |
| `max_hr` / `lthr` / `ftp` | separate axis | Tolerance-checked against the macrocycle snapshot, unchanged (`DESIGN_benchmark_workouts.md` §3.3). |

## 4. The two mixed bags

**`preferences`** is one opaque blob holding two levels at once. From the sample config:
*"keep 2 sessions/week through every phase, including taper"* is a periodization directive,
while *"I consistently over-do sessions that are prescribed easy — call this out"* is
coaching tone and *"a kettlebell circuit once in a while is welcome"* is session flavor.

Nothing in the field distinguishes them, so it stays in and it will over-trigger: adding a
sentence about tone re-flags a good plan. Accepted deliberately over the alternative of
having an LLM judge whether a prose diff is plan-relevant — nondeterministic, costs a call,
and its failure mode is silently *under*-triggering on an injury edit.

**`weekly_schedule`** is fingerprinted per sub-key rather than whole:

- `total_available_hours`, `max_sessions` — in. A weekly ceiling and session density *are*
  load structure.
- `certainty_percent` — in. It steers how much load the plan dares commit to a given day.
- per-day `equipment` — out. Swapping Tuesday's kettlebells for a rower changes what
  Tuesday is, not the block structure.

## 5. Naming the field

`config_hash` answers *did something change*; it cannot answer *what*, because a hash is
one-way. So the macrocycle now also carries `profile_snapshot` — the plan-shaping profile
fields as JSON, the same shape `config_snapshot` already had for thresholds. The reason
becomes `"athlete profile changed: chronic_injuries, sport_preferences"`.

A field is named whether it was added, removed, or edited. The athlete needs to know which
input to go look at, not which of the three happened to it.

The reason falls back to the bare `"athlete profile changed"` when the snapshot is missing
or unreadable — a plan generated before the column existed, and the one-off case in §7.
Both are cases where naming fields would be guessing.

This is the *messaging* half of the fix and arguably the larger one: over-triggering that
explains itself is a glance, not an interruption.

## 6. Why the exclusions stay a denylist

`plan_profile()` still starts from "everything in `user_profile`" and subtracts. A profile
field added later therefore counts as plan-shaping until someone decides otherwise. An
allowlist would fail the other way — a new plan-relevant field silently ignored, and no
symptom until a plan quietly fails to notice it. The safe failure direction is the noisy
one.

## 7. Upgrade behaviour

Every macrocycle that predates this change flags stale exactly once. Its `config_hash` was
computed over the *old* partition (which included `name` and `equipment`), so it cannot
match a hash over the new one even when nothing was edited, and it has no
`profile_snapshot` to attribute the difference to.

Declining the proposal re-stamps the hash and both snapshots (`plan generate` /
`workout generate`), so it is self-healing after one prompt. Backfilling the snapshot at
migration time was rejected: the honest value is what the plan was generated with, which is
not recoverable, and writing the *current* profile would erase a genuine pending change.

## 8. Deliberately not done

- **`plan diff` does not show profile changes.** The snapshot is now there and a version
  diff could render it beside goals/constraints/thresholds, but that is display surface
  (CLI renderer + web JSON + their tests) and independent of staleness. Natural follow-up.
- **The web dashboard banner still does not name fields.** `trainmate_web.py` compares
  `plan_config_hash()` directly to avoid importing the engine (ARCHITECTURE.md §8); the
  helpers it would need (`changed_plan_profile_fields`) live in `config.py` and are
  reachable, but the banner is read-only and was left alone.
- **The triplicated staleness-confirm flow stays triplicated.** `plans.py`,
  `workouts/generate.py` and `status.py` each render and stamp their own way — design smell
  B-8 (`design_audit/design_smells_2026-08-06.md`). It is why `status.py` warns without
  offering to dismiss. Out of scope here; the partition change touched all three call sites
  without making the duplication worse.
