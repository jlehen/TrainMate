# Design: Plan feedback as a captured log (`plan feedback`)

Redesign of the `plan feedback` command: selector-free capture into an append-only log, optional
human-addressable filing, no LLM call anywhere in the capture path. Replaces the
`--macro`/`--meso ID` overwrite-slot interface.

## 1. Motivation

Four defects, one command:

1. **Routing in database coordinates.** `--meso 7` requires a `plan show` detour to learn that
   "Climb-Specific Transmutation" is row 7, and "either `--macro` or `--meso` is mandatory" makes
   the athlete file the note before they can say it. The block has a name; its address should not
   be an autoincrement integer.
2. **A grammar the CLI already outgrew.** `--macro`/`--meso` predates DESIGN_cli_selectors.md and
   was never migrated, so the one command that should read most like talking to a coach is the one
   that reads most like database administration.
3. **An overwrite slot.** Feedback is one `UPDATE … SET feedback = ?` per macro/meso row: a second
   thought silently replaces the first, and merging two thoughts means `--edit` plus hand-editing.
   Telling the coach something should be additive.
4. **The `--force` wart.** Saved feedback does not count as a plan-input change, so the command
   ends by instructing `plan generate --force`. Feedback *is* a change to the plan's inputs;
   needing `--force` to apply it is the regeneration gate misfiring.

And the fact that unlocks the redesign: at regeneration time,
`trainmate/coach/service/planning.py` concatenates **all** feedback — macro plus every mesocycle,
labeled by phase — into a single prompt section the planner reads whole. Routing at capture time
therefore decides a label and a storage row, never what the model considers. The current interface
demands careful filing for a reader that reads the whole cabinet anyway.

## 2. Mental model

A feedback note is a message **addressed to the next plan version**, written while the current one
is in force. Notes accumulate against the active macrocycle; `plan generate` reads them all;
applying the new version consumes them, in the sense that they stay attached to the now-superseded
macrocycle as history. `plan rollback` restoring a superseded version brings its notes back to
pending — intended: rolling back abandons the version that addressed them, so their concerns
reopen.

**No LLM at capture.** The understanding step belongs to the regeneration call, which happens
anyway and is the only consumer of the result. This is the same-call folding that
`workout adapt --message` already established (the note is "passed to the SAME LLM" doing the
adaptation — trainmate/coach/service/adaptation.py). Capture itself is an INSERT: milliseconds,
offline, nothing to misroute. Consequence: the echo confirms the note was *recorded*, not
*understood*; a misreading surfaces at the gate that already exists for it — `plan generate`'s
preview-and-confirm.

Where this sits among the real-world-context channels (the boundary lines do not move):

| Channel | Carries |
| --- | --- |
| `constraint add` | Directives the coach must work around |
| `context add` | Observations (alcohol, poor sleep, stress) that explain mornings |
| `workout adapt -m` | In-the-moment capture, classified into constraint or one-off nudge |
| `plan feedback` | **Opinions about the plan itself**, consumed at the next regeneration |

## 3. Goals / Non-goals

Goals:

- Capture with no selector, no flags, no IDs: `tm plan feedback "…"` and you have your prompt back.
- Optional explicit filing addressed the way the athlete thinks: block name, date, or bare `-m`
  for the current block. IDs keep working.
- Append semantics with visible history and per-note removal.
- Pending feedback counts as a regeneration trigger; `--replan` collapses the two-step entirely.
- Consumption quality unchanged or better: the regen prompt still sees everything, now dated and
  ordered.

Non-goals:

- **No LLM routing at capture** (§11: latency it can't pay for, buying a label the regen ignores).
- **No unified inbox** (`tm coach "…"` classifying nudge/constraint/feedback in one place). That
  is the plausible end-state and this log is its natural substrate, but it reorganizes the channel
  taxonomy and deserves its own design (DESIGN_capture_inbox.md, unwritten).
- **No range-grammar extension across the CLI.** The new `-m` atoms (§5) are defined in the shared
  selector module so every command can adopt them later, but rewiring `resolve_window` and its
  consumers is a follow-up, not this change.
- **No web write path.** The dashboard stays read-only; it re-renders what changed (§8).

## 4. Command surface

```
plan feedback                          List pending notes for the active goal's plan
plan feedback "text"                   Append a plan-level note
plan feedback "text" -m               Append a note filed to the current block
plan feedback "text" -m ATOM          … filed to the block ATOM resolves to (§5)
plan feedback --rm ID [-y]             Delete one pending note (y/N confirm unless -y)
plan feedback … -g ID                  Target another goal's plan (default: soonest active goal,
                                       matching today's behavior)
plan feedback "text" --replan          Append, then enter the regeneration flow immediately
```

Removed, deliberately:

- `--macro` — bare text *is* plan-level; there is nothing left for the flag to say.
- `--meso ID` — folded into `-m` (§5), which still accepts the ID.
- `--edit` — curating a log in `$EDITOR` is fiddly, and the editor does not exist over the
  Telegram bot. Rewording is `--rm` + re-add. Revisit only if genuinely missed.

Echoes, exactly:

```
Noted [id 12, plan-level]: "the Friday sessions should progress duration, not surges"
2 notes pending — they feed the next plan generate (--replan runs it now).
```

with `[id 13, filed: Climb-Specific Transmutation]` for a filed note. The bare listing prints
`[id] date · plan-level|<block name> · text`, **oldest first** — one ordering everywhere (listing,
`plan show`, prompt), so the log always reads as a conversation in the order it happened. Empty
listing prints one line saying how to add a note.

Fit with DESIGN_cli_noargs.md: the bare run is read-only listing (bucket 1); `--rm` with no ID
gets the §a missing-argument treatment. Over the bot every form is one-line and editor-free;
`--rm` follows the destructive-command rule (declined without `-y`, since the bot cannot confirm).

`--replan` runs the same flow as `plan generate` for that goal after saving — preview, then a
human `y`, per the constraints precedent that nothing regenerates a plan without one
(trainmate/cli/constraints.py §7 note). It does not imply `--force`; it does not need to (§7).

Conflict rule: `-g` picks the plan first; `-m` resolves inside it. A mesocycle ID that belongs to
a different goal's plan errors naming the goal it belongs to.

## 5. The `-m` atom, humanized

`-m [ATOM]` names **one** block; range spellings (`3..5`) are rejected by name — a note files to
one block. The atom set extends the shared grammar's "a mesocycle ID":

| ATOM | Resolves to |
| --- | --- |
| bare integer | Mesocycle ID (a bare number is never a date — DESIGN_cli_selectors.md §1); must belong to the target plan |
| date atom: `2026-09-05`, `today`, `-7d`, `+2w` | The block whose start–end range contains that day |
| anything else | Case-insensitive infix of a block *name*; must match exactly one |
| *(absent)* | The block containing today — same meaning bare `-m` already has in the shared grammar |

Date atoms reuse the `-d` atom parser. An unsigned span (`7d`) is rejected by name: it describes a
window, not a day. A name infix matching zero or several blocks errors and lists the plan's blocks
(name + dates) to retry against — an error listing, not an interactive picker, so the bot behaves
identically.

All resolution happens against the **active** macrocycle of the target goal. Filing to a
superseded version is meaningless for steering the next one, so a mesocycle ID outside the active
plan errors (this tightens today's unchecked `get_mesocycle`).

The resolver lives in `trainmate/cli/selectors.py` beside the range machinery, as a single-atom
function the range grammar can lift the day every command wants `workout list -m climb`. Add a
forward-note to DESIGN_cli_selectors.md §1 that `-m` atoms gain dates and name-infixes here first.

## 6. Data model & migration

```sql
CREATE TABLE plan_feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    macrocycle_id INTEGER NOT NULL REFERENCES macrocycles(id),
    mesocycle_id  INTEGER REFERENCES mesocycles(id),   -- NULL = plan-level
    created_at    TEXT NOT NULL,                        -- ISO-8601 UTC, full precision
    text          TEXT NOT NULL
);
```

When `mesocycle_id` is set, `macrocycle_id` is that mesocycle's parent — stored anyway, because
every read is per-plan and the join buys nothing.

**Pending** := rows whose `macrocycle_id` is the goal's active macrocycle. There is no consumed
flag: **supersession is the consumption event.** A regeneration that is previewed but declined
leaves the macro active and the notes pending — correct, nothing consumed them. Rollback makes a
superseded macro active again and its notes return to pending (§2).

Migration — one-off, per AGENTS.md (single user, no backward-compat scaffolding):

1. Bump `SCHEMA_VERSION`; create the table.
2. Backfill each non-empty `macrocycles.feedback` / `mesocycles.feedback` as one row.
   `created_at` := the owning macro's `created_at` (mesocycles carry none; the parent's is the
   best available and only orders a one-off backfill).
3. `ALTER TABLE macrocycles DROP COLUMN feedback` and likewise on `mesocycles` (precedent: the
   constraints table dropped `binding`/`sport`/`type` the same way).
4. `trainmate/types.py`: drop the two `feedback` fields; add a `PlanFeedback` TypedDict.

## 7. Consumption

**Gate.** `plan_generate` currently skips when an active macro exists, `--force` is absent, and
the goals/constraints/config hashes all match. OR one more disjunct into that test: *pending
feedback exists*. Notes are plan inputs; their presence makes regeneration proceed without
`--force`. `_FEEDBACK_REGEN_NOTE` and its `--force` advice die with this — the append echo (§4)
replaces them.

**Prompt.** In `trainmate/coach/engine/planning.py`, the `### ATHLETE FEEDBACK ON THE PREVIOUS
PLAN` section becomes:

```
### ATHLETE FEEDBACK ON THE CURRENT PLAN
Verbatim notes from the athlete about the plan in place, oldest first. A note marked
(phase: <name>) was filed against that mesocycle; unmarked notes address the plan as a whole.
- [2026-08-12] (phase: Climb-Specific Transmutation) "the Friday sessions should progress
  duration at 95-100% instead of surges"
- [2026-08-13] "drop the second FTP test"
You MUST address every note: revise the macrocycle strategy and/or the duration, boundaries,
and focuses of individual mesocycles accordingly (e.g. scheduling more rest, changing block
emphasis, extending/shortening specific cycles), while continuing to respect overall sports
science principles and guidelines.
```

Oldest first so later notes read as amendments of earlier ones ("actually, keep the second test").
Filed notes carry the phase **name** — names survive version churn; IDs do not. The service layer
(`trainmate/coach/service/planning.py`) assembles the list from pending rows joined with mesocycle
names, replacing today's slot-reading block.

**Not consumers.** `workout generate` and `workout adapt` keep reading only the mesocycle focus
text and constraints. A note reshapes workouts *by reshaping the plan first*; a note that must
skip the plan is what `workout adapt -m` is for (§12 revisits).

## 8. Display

- **`plan show`**: the two per-slot renders (macro block + per-meso indented text) become one
  `ATHLETE FEEDBACK` section after the strategy: `[id] date · plan-level|<block name> · text`,
  oldest first, qualified "(pending — feeds the next plan generate)" when the shown macrocycle is
  active and "(consumed by the successor version)" when superseded.
- **`plan diff`**: the feedback panel stops prose-diffing a slot and lists each side's attached
  notes; for adjacent versions old→new that reads as "what drove the change".
  `trainmate/plan_diff.py`, shared with `/api/plan/diff`.
- **`status`**: when pending notes exist, one line near the plan section:
  `Plan feedback: 2 pending — plan generate will address them.`
- **Web dashboard** (read-only, unchanged contract): the `strategy-feedback` and
  `cycle-details-feedback` panels render the pending list (date, filing, text) from the same DB
  helper; the `app.js` explanatory note updates.

## 9. Code touch points

Per house convention, code carries a one-line why plus a §-pointer to this document; the rationale
is not restated in comments.

| File | Change |
| --- | --- |
| `trainmate/cli/plans.py` | Parser rebuilt (§4); `run_plan_feedback` rewritten around append/list/rm/replan; `plan show` + versions rendering (§8); `_FEEDBACK_REGEN_NOTE` removed |
| `trainmate/cli/selectors.py` | Single-target mesocycle atom resolver (§5) |
| `trainmate/db/periodization.py` | `update_macrocycle_feedback`/`update_mesocycle_feedback` replaced by `add_plan_feedback` / `list_plan_feedback(macrocycle_id)` (joined with meso names) / `rm_plan_feedback` |
| `trainmate/db/base.py` | Migration §6 |
| `trainmate/types.py` | Drop `feedback` fields; add `PlanFeedback` |
| `trainmate/coach/service/planning.py` | Regen gate disjunct; pending-notes prompt assembly (§7) |
| `trainmate/coach/engine/planning.py` | Section text (§7) |
| `trainmate/plan_diff.py` | Feedback panel → note lists (§8) |
| `trainmate/cli/status.py` | Pending-count line (§8) |
| `trainmate_web.py` + `static/app.js`/`index.html`/`style.css` | Render pending list (§8) |
| `README.md` | Steering-channels table row + the `plan feedback` mentions |
| `ARCHITECTURE.md` | Schema tables (macrocycles/mesocycles rows → `plan_feedback` table), command table rows for `plan feedback`/`plan diff`, prose mentions of the feedback flow |
| `tests/test_feedback.py` | Rewritten (§10) |

## 10. Tests

`tests/test_feedback.py`, rewritten around the log:

- Atom resolution: ID inside/outside the active plan; date atoms including signed offsets; span
  (`7d`) rejected; name infix unique / ambiguous / missing; bare `-m`.
- Append / list ordering / `--rm` with and without `-y`.
- Lifecycle: pending → consumed on generate-supersede; declined preview leaves pending; rollback
  resurrects.
- Migration: backfill of both slot kinds, timestamps, columns dropped.
- Prompt assembly: ordering, phase labels, section absent when no notes pending.
- Gate: pending notes ⇒ regeneration proceeds without `--force`; none ⇒ the unchanged-inputs skip
  still holds.
- Bare no-args run lists and writes nothing; `--replan` reaches the confirm gate and writes no
  plan on `n`.

## 11. Alternatives considered

- **LLM routing at capture.** Adds 2–10 s (one model menu, no fast tier — DESIGN_model_selection)
  to a command that must feel instant, to buy a label the regeneration ignores (§1). Rejected on
  latency. The log leaves the door open: a later change could file pending notes as a rider on a
  call that already happens (the nightly adapt), the same-call pattern again — cosmetic only, so
  it can wait indefinitely.
- **Keyword/date heuristics at capture.** Free, but a heuristic that guesses wrong files the note
  silently under the wrong block; labels are not worth brittleness. Explicit atoms only.
- **Keeping `--edit`.** Editor curation of a multi-entry log is fiddly and bot-hostile;
  `--rm` + re-add covers rewording.
- **A `next` keyword atom.** Collides with plausible block names; `+2w` says it deterministically.
- **Unified inbox (`tm coach`).** The right end-state; out of scope here (§3). This table is its
  substrate.

## 12. Open questions

- Should `workout generate` inject pending notes filed to the blocks it is generating for? Lean
  no: it blurs the "notes reshape workouts via the plan" line (§7) and adds a second consumer with
  its own lifecycle questions. Revisit if the feedback → `plan generate` → `workout generate`
  two-step proves heavy in practice.
