# Design: The Calendar event carries the session's whole lineage

**Status:** Implemented · **Date:** 2026-08-23 · **Branch:** worktree-workout-revisions-design

## 1. The problem

A Google Calendar event is where the athlete actually meets the plan. It is the surface
open on the phone at 6am, and for a session that has been rewritten it is the only surface
that has to answer "what was this supposed to be, and why is it this instead?" without a
terminal.

Today the event answers that with two forms and a number:

```
Duration: 65m | TSS: 80

Adapted:
Easy aerobic ride, keep it conversational.

Originally:
3h steady endurance, second half at tempo.

Reason:
HRV suppressed three mornings running.

Originally: 180m | TSS 210
Planned: 2026-08-01 06:00 · Last adapted: 2026-08-30 06:00 · Adapted ×2
```

"Adapted ×2" says the session was walked down twice. The event then shows the *first* form
and the *last* form and nothing in between, so the middle form — the one that says whether
the block eased gradually or fell off a cliff — is missing. `Reason` is the newest reason
only; the earlier one is gone. And the two "Originally" lines say the same thing twice, in
different vocabulary, one as prose and one as numbers.

That shape was forced by the old table: a workout row held one `original_description` and
one `original_duration_minutes`, so two forms were all there ever were. `workouts` is now an
append-only revision log (DESIGN_workout_revisions.md), every form of the session is a row,
and the lineage links them. The event can show the whole story and there is no longer a
reason for it not to.

## 2. Decision

**A Calendar event whose session has more than one revision carries a `History` block:
every earlier revision of the lineage, newest first, each as its own labelled entry with
its date, load, intensity target, reason and body.**

The current prescription keeps the top of the event — that is what the athlete is about to
go and do, and it must not become one entry among many that has to be searched for. The
history sits below it, under a separator, in the order the athlete asks the question:
*what was this before?* — most recent first, walking backwards.

A session with one revision renders exactly as it does today. Nothing about an ordinary
generated week changes.

## 3. What one entry shows

```
─────────────────────────────
History · 3 earlier revisions, newest first

[3/4] Adapted · 2026-08-30 06:12
      2026-09-01 Tue · Long ride
      Duration: 120m | TSS: 150 | RPE: 7
      Target: ~30min recovery, ~60min aerobic, ~20min threshold
      Reason: Eased from 180m — sleep debt across the week
      Change: HRV suppressed three mornings running
      2h steady endurance, last 20min at tempo.

[2/4] Moved · 2026-08-25 09:40
      2026-08-31 Mon · Long ride
      Reason: Swapped with Tuesday's easy run

[1/4] Planned · 2026-08-01 06:00
      2026-08-31 Mon · Long ride
      Duration: 180m | TSS: 210 | RPE: 8
      Target: ~40min recovery, ~90min aerobic, ~50min threshold
      3h steady endurance, second half at tempo.
```

Line by line:

| Line | Source | Shown when |
|---|---|---|
| `[3/4] Adapted · <when>` | position in the lineage, the change's `kind`, the change's `created_at` | always |
| `2026-09-01 Tue · Long ride` | the revision's `date` and `title` | always |
| `Duration: … \| TSS: … \| RPE: …` | the revision's load columns | any of the three is set, and the revision is not a void |
| `Target: …` | `intensity.format_planned_zones` over the revision's planned-zone columns | the revision carries a target, and is not a void |
| `Reason: …` | the revision's own `reason` — why *this session* moved | set |
| `Change: …` | the change's `summary` — why the *command* ran | set, and different from `Reason` |
| the body | the revision's `description` | set, and the revision is not a void |

The kind is rendered as a verb in the past tense, because an entry is an event in the
session's life, not a category:

| `kind` | live revision | void revision |
|---|---|---|
| `generate` | Planned | Dropped from the plan |
| `adapt` | Adapted | Dropped by the adaptation |
| `accommodate` | Accommodated | Dropped to fit a constraint |
| `swap` | Moved | Moved away |
| `add` | Added by hand | — |
| `rm` | — | Cancelled |
| `restore` | Restored | — |
| `rollback` | Rolled back | Undone |
| `stand-down` | — | Goal stood down |
| `reinstate` | Reinstated | — |

**A void entry is rendered lean** — label, date, title, reason, and nothing else. A void
copies the departing session's columns forward (DESIGN_workout_revisions.md §3), so it
still *carries* a duration and a target; printing them would state a prescription for a day
that holds no session.

That is what makes a swap legible. A cross-sport swap leaves the moved session with a void
where it left and a copy where it landed (DESIGN_workout_revisions.md §4), and both are
revisions of one lineage, so the history reads `Moved` at the new date, then `Moved away`
from the old one, then the form it had before the move.

## 4. Which revisions, and in what order

Every revision of the lineage except the one the event is currently rendering, newest
first. Not a filtered subset:

- **voids are in.** A cancel-then-restore is a real thing that happened to the session and
  the gap is exactly what an athlete would ask about.
- **`restore` and `rollback` revisions are in**, as themselves. The §7 tally walk jumps
  over an undone span so it does not count easings that were taken back; the history is the
  opposite job — it is the record of what happened, and an undo happened.
- **the first revision is in**, and it is the one carrying the load the footer used to call
  `Originally:`.

The lineage is read whole, at render time, from the log. There is nothing to store and
nothing to keep in step: the block is a pure function of rows that can never be edited.

## 5. What it replaces

Three things in the current event become duplication the moment the history block exists,
and two of them go:

1. **The `Adapted:` / `Originally:` pair in the body.** Its condition is
   `original_description != description`, and under the revision model
   `original_description` *is* the first revision's description
   (DESIGN_workout_revisions.md §7) — so the branch can only fire when the lineage has more
   than one revision, which is exactly when the history block renders the first revision in
   full, with its date and its load. The body at the top is now just the current
   description, with `Reason:` under it.
2. **The `Originally: 180m | TSS 210 | RPE 8` footer line.** Same numbers as the oldest
   history entry, minus the date and the target.
3. The `Planned: … · Last adapted: … · Adapted ×N` lifecycle line **stays**: it is a
   summary, not a repetition, and `Adapted ×2` is not derivable by counting entries — the
   tally deliberately ignores easings that were later undone and adaptations that did not
   actually lower the load (DESIGN_workout_revisions.md §7).

## 6. Freshness — the signature has to see the lineage

`pushed_signature` is a hash of exactly the fields that determine the rendered event
(`calendar_state.CALENDAR_FIELDS`), and an event is re-pushed when the hash moves. The
description now depends on the whole lineage, so the hash has to move whenever the lineage
grows.

**`revision_id` joins `CALENDAR_FIELDS`.** It is the id of the live revision the event
speaks for, it is assigned by the log and never reused, and a lineage only ever changes by
gaining a revision — so `revision_id` moves exactly when the history block does. Nothing
else in the field list has that property. Two holes close with it:

- **an unpushed adapt, then a rollback.** The rollback appends a copy of the pre-adapt
  revision, so every content field returns to the value that was last pushed and the event
  reads `synced` — while the lineage has grown by two revisions the event does not show.
- **a regeneration that only moves the intensity target.** The planned-zone columns are
  deliberately outside `CALENDAR_FIELDS` (DESIGN_intensity_distribution.md §9.8), so today
  a regeneration that nudges the target leaves the event reading `synced` with a stale
  `Target:` line. It writes a revision — zones are in `PRESCRIPTION_FIELDS` — so
  `revision_id` catches it.

**`rpe` joins the list too**, and the comment above it is corrected. The list is excluding
`rpe` on the grounds that it "never reaches Calendar", which stopped being true when the
event grew its `Duration: 20m | TSS: 15 | RPE: 4` line: an RPE edit changes the rendered
event and has been leaving it reading `synced`. `revision_id` would cover it, but a field
list whose stated contract is *exactly what the event renders* should not be missing one of
them.

One consequence to expect once: the first `workout push` after this ships re-sends every
session that has ever been revised, because their descriptions genuinely changed.

## 7. Size

Google caps an event description at 8192 characters. A lineage is normally a handful of
revisions, but nothing bounds it — each `workout generate` over a future week appends a
revision to every session it changes, and a long block regenerated often could accumulate
dozens.

The history is therefore **rendered last, into the room the rest of the event did not
need**. The current prescription, its reason, the target and the footer are laid down
first and in full; the history takes what is left, up to a standing ceiling of its own.
Two limits, and the tighter one wins:

| Limit | Value | Why |
|---|---|---|
| `MAX_DESCRIPTION` | 8192 | Google's hard cap. Exceeding it is an API error, so the history has to yield before anything else does — the athlete's actual prescription is never the thing that gets cut. |
| `HISTORY_BUDGET` | 4000 | The event is something to read on a phone, not an archive. The history yields well before the API would make it. |

Entries are rendered newest first while they fit; the rest are replaced by one line:

```
… 14 earlier revisions not shown.
```

Dropping the *oldest* is the right end to drop from: the recent forms are the ones the
question is usually about, and the oldest form's numbers also survive in the `Planned:`
lifecycle line. Truncation is stated rather than silent, because an event that quietly
stops at revision 6 of 20 reads as a complete history — and that line has to fit too, so
one more entry gives way to it rather than the other way round.

## 8. Tests

- an adapted session's event contains its earlier form's date, load, target, reason and
  body, and the entries run newest first;
- a session with one revision renders with no `History` block at all — the ordinary case
  is untouched;
- a swapped session's history names both the destination and the vacated date;
- a void entry prints no `Duration:` and no `Target:`;
- a `Change:` line appears only when the change summary differs from the revision's reason;
- appending a revision marks the event stale — the rule spans `db/workouts.py` and
  `calendar_state.py`, so the test spans them too (AGENTS.md);
- a lineage past the budget renders the newest entries plus the count of what was dropped,
  and stays inside the budget.

## 9. Deliberately not done

- **The web UI keeps its one-line `Originally:`.** `static/app.js` renders a card, not a
  document, and the terminal already has `workout batches` for history. If the card should
  grow a history it should grow a collapsible one, which is its own change.
- **No per-entry diff.** An entry states what the session *was*, not what changed against
  its predecessor. A diff would need a vocabulary for every field, and reading two adjacent
  entries answers the same question.
- **No HTML.** Google Calendar accepts a small subset of HTML in descriptions, but the
  event body is also read through notification emails and mobile widgets that show it as
  plain text. Plain text renders the same everywhere.
