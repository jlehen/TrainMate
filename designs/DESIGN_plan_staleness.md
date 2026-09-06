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
| `gender` | yes | Sex-specific physiology (hormonal cycle, substrate use, injury risk) belongs in the block structure, not just the session detail. Like `birth_year` it changes rarely, so over-triggering costs nothing. |
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

- **`plan diff` does not show profile changes.** (The staleness surfaces now do — §10.)
  The snapshot is there and a version
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
  without making the duplication worse. **Resolved in §9.**

## 9. Saying what the flag means, and where to find it

**Date:** 2026-09-03 · **Branch:** worktree-staleness-prompt

§5 made the flag name the fields that moved. Using it still needed two things it did not
have.

**The prompt stated a fact and asked for a decision, with nothing in between.** "A
plan-shaping input has changed (athlete profile changed: preferences). Regenerate?" is
only answerable by someone holding §2's test in their head. And because `preferences` is
one blob that over-triggers by design (§4), the athlete meets this question most often in
exactly the case where the answer is *no* — a reworded sentence about how sessions should
be described. So the test is now printed with the question:

> Regenerate only if the change would have altered the block structure, phase order or
> volume ramp. Wording, tone or how sessions are described: keep the plan — your next
> `workout generate` picks it up anyway.

The second sentence is the load-bearing one, and it was missing everywhere. The profile
reaches *every* prompt through `_format_athlete_profile`, so a session-level edit lands at
the next workout generation whether or not the periodization is rebuilt. Without that,
"keep the plan" reads as "discard what you just wrote".

**The flag had no home.** It fired in `status` — the athlete overview — and in the two
generate paths, which are where you go once you have already decided. `plan show`, the
command whose whole job is to show a plan and the inputs it was generated from, did not
check at all. So the notice appeared where it could not be acted on and was absent where
it would be looked for.

It now prints in `plan show`, immediately under the `Inputs considered` block it
contradicts, naming both routes out. `status` keeps a one-line mention and points there
rather than at `plan generate`: an overview reports, it does not adjudicate a replan.

**`plan keep` is the second route.** Declining at the `plan generate` prompt was the only
way to clear a false alarm, which meant invoking a command that otherwise proposes a whole
new periodization and spends a strategy call in order to say that nothing needs doing.
`plan keep` re-stamps the hash and both snapshots, prints what changed, and says the edit
still reaches the sessions. Same write as the declined branch — one behaviour, three
entry points, which is what makes it explicable.

**One owner.** All of it lives in `cli/staleness.py`: `reason`, `guidance`, `stamp`,
`kept_line`, `confirm_regenerate`, `report`. The four surfaces call it rather than each
phrasing and stamping their own way, which closes B-8. The wording had to stop being
copied before it could get longer — three call sites carrying a two-sentence explanation
between them is how they drift.

**Two exemptions.** A *superseded* version is out of date by definition, so `plan show
--macrocycle <old>` does not flag it; the notice is about the plan in force. And the
companion voice draws nothing — regenerating is operator work, and the companion athlete
has no shell to run either command in (DESIGN_render_persona.md §5), the same silence
`runway_hint` already takes.

## 10. Showing the edit, and asking the coach

**Date:** 2026-09-06 · **Branch:** worktree-staleness-diff-verdict

§9 put the §2 test next to the question. The athlete still had to apply it to a field
name. "preferences changed" is one blob, and the change that triggered this section was
a rewrite of that blob in which most lines were motivation and one line dropped a
session-placement preference. Judging that from the word `preferences` is guesswork.

**The diff.** Every surface that names the change now shows it: a unified diff per
changed plan-shaping field, old against new, rendered from the same `profile_snapshot`
§5 introduced. Prose fields diff as prose; structured ones (the weekly schedule) as
sorted JSON, so a reordered dict is not a change. Threshold drift shows no diff — its
reason already carries the numbers. One renderer in the service (`profile_diff`), one
printer in `cli/staleness.py`, and `plan show`, `plan keep` and both questions use it.

**The coach's read.** Before either question the coach is asked the §2 test itself: given
this diff and the plan as it stands, would you have built a structurally different
periodization? It answers `{"reshaping": bool, "why": str}` and the line prints as
`Coach: keep the plan. …` or `Coach: re-shaping. …`.

- *Small on purpose.* The rubric, the diff, the strategy text and the block list. No
  science file, no history, no metrics: the question is structural, and everything the
  athlete's state would add is already baked into the plan being judged. First run on a
  real instance: ~5k prompt tokens.
- *Strict on purpose.* A model told to look for a reason to regenerate finds one. The
  rubric names the three things that count, lists what does not (wording, tone,
  motivation, what to listen to, which days), and demands the concrete structural change
  it would make — "cannot name one" is keep.
- *On the coach's model, not the router's.* The verdict predicts what the coach would do.
  The router model exists because bot routing runs on every message and must be cheap;
  this runs only when an input changed. The gain from using the same model is
  consistency between the prediction and the regeneration, not accuracy — the model has
  no memory of building the plan and sees only what the prompt shows it. The decisive
  reason is simpler: it is the default, so a pinned `--llm-model` applies unchanged and
  there is no second model to explain.
- *Fails open.* Any error or malformed reply prints an aside and leaves the athlete with
  the diff, the guidance and the question, exactly as §9 had it. The network never
  stands between the athlete and the question.
- *Advisory, and the default follows it.* The human still answers. But a coach that says
  "re-shaping" over a question defaulting to No is two answers on one screen, so `plan
  generate` defaults to Yes on a re-shaping verdict, and `workout generate`'s "proceed
  anyway?" — where proceeding is the keep answer — defaults to Yes on a keep verdict.
  No verdict, no change: the old defaults stand.

**Where it is not asked.** `plan show` and `plan keep` print the diff and nothing more: a
read-only command makes no network call, and `keep` is already the answer. `status` is
unchanged. The `--show-llm-prompt-only` flag shows the command's own prompt, so the
verdict steps aside under it rather than printing its prompt and exiting first.

**Deliberately not done.** No caching of the verdict on the macrocycle: both questions
stamp on "keep", so the same change is asked about once. The web banner still names
fields only. The bot has no path to this question (§9's companion exemption stands).
