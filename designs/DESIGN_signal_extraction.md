# Design: Extracting Daily Signals From `workout adapt --message`

**Status:** Implemented · **Date:** 2026-08-30 · **Companion to:**
`DESIGN_constraints.md` §8, `DESIGN_signal_authoring.md`,
`DESIGN_quantitative_signal_impact.md`

`adapt --message` already extracts durable **constraints** from the athlete's note. This
adds the second thing a note can carry: a **daily signal** — something that happened to
the athlete on particular days, which helps explain their recovery readings.

---

## 1. Motivation

The constraint-extraction prompt has a bucket it deliberately throws away:

> A note only about how they feel right now ("felt flat, ease today") is NOT durable —
> leave "new_constraints" empty for it.

A good part of that bucket is what daily signals are *for*. "Three beers last night" and
"the kid was up all night" are not constraints — the coach does not plan around them — but
they are exactly the external causes `DESIGN_quantitative_signal_impact.md` aligns against
HRV/RHR/sleep. Today they evaporate after one run. As signals they become durable evidence
the weekly analysis can use.

Nothing about the storage changes. The extraction produces the same rows `signal add`
produces, through the same calendar write.

---

## 2. Mechanism — a third list on the same LLM call

`_workout_adapt_logic` gains a `new_signals` member beside `new_constraints`, gated on the
same `has_message` flag, extracted by the same call. No extra round-trip, no extra cost.

```
"new_signals": [
  {
    "metric": "alcohol",      // required, the category
    "date": "YYYY-MM-DD",     // required, first day it acted on
    "end_date": "YYYY-MM-DD", // required, == date for a single day
    "value": 3,               // only when the note stated a number
    "text": "three beers"     // optional
  }
]
```

**The confirmation flow gains a step 1b.** `DESIGN_constraints.md` §8 confirms constraints
before the adaptation preview; signals are confirmed straight after them, each candidate
its own `[y/N]`, still before the preview. Independent commits, as before: declining a
signal does not block the adaptation, and vice versa.

**A signal confirmed here informs the NEXT run, not this one.** The LLM call that proposed
it has already returned. This is already true of constraints, but it is more
counter-intuitive for signals, which look like they should explain today's numbers. They
explain them from tomorrow, and in the next weekly analysis.

**Discrimination rule**, stated once in the prompt: a CONSTRAINT is a rule to plan around;
a SIGNAL is an external cause acting on the body on given days. A note that is only a mood
report with no cause is neither, and stays the ephemeral nudge it is today.

---

## 3. Trust boundary — the model may pass a number, never invent one

`DESIGN_constraints.md` §8 draws the line at *reversible, advisory state*: an extracted
constraint is always `rest=0`, `replan=0`, enforced server-side whatever the model said.

Signals need a second line, because a signal carries **data**, not a directive. `value`
feeds `_signal_days` as a dose magnitude. A model that turns "slept badly" into
`"value": 7` has invented a measurement and dressed it as observation.

So: **`value` is set only when the note states a number**, in the unit the category's
description names. The prompt says it; `capture_message_signal` enforces what it can —
anything that is not an `int`/`float` (a bool included) is dropped rather than coerced. A
category with free text and no number is complete and useful; `_signal_days` handles
valueless days already.

The category itself is normalized — trimmed and lowercased — at every write path.
`daily_signals.metric` has no `COLLATE NOCASE`, so `Heat` and `heat` would otherwise
`GROUP BY` into two categories. This was already reachable by hand via `signal add`.

---

## 4. The `sleep` coupling

`SIGNAL_CHANNEL_EXCLUSIONS` drops a response channel that measures the same construct as
the signal, so the analysis cannot "discover" that bad sleep predicts bad sleep. It matches
on a **substring** of the category name:

```python
("sleep", {"sleep"})
```

That makes the spelling load-bearing. `bad_sleep`, `poor_sleep` and `disturbed_sleep` all
trip the guard. **`insomnia` does not** — and the failure is silent: the analysis would
correlate insomnia-days against the measured sleep score and report the circularity as a
finding.

Two consequences, both acted on:

1. The shipped sleep category is **`disturbed_sleep`** — it contains the substring, and it
   covers both "couldn't get to sleep" and "was woken by something", which `insomnia` does
   not.
2. The vocabulary and the exclusion table now live in the **same file**
   (`trainmate/signals.py`), moved out of `coach/service/analysis.py`. They were in
   different layers, which is how someone renames a category without noticing the guard.
   `config_template_full.yaml` repeats the warning where a category is actually named.

---

## 5. The vocabulary — shipped core, config augments

A category is an exact-match clustering key in `_signal_days`. `heat` on Monday and
`heatwave` on Tuesday produce two one-day episodes instead of one two-day episode. No
error, just a quietly broken dose/response alignment. So the model is shown the categories
already in use and told to reuse them.

**Shipped** (`signals.DEFAULT_SIGNAL_METRICS`): `alcohol`, `disturbed_sleep`, `illness`,
`heat`, `stress`, `travel`, `underfuelling`. **Config** (`coach.signal_metrics`) augments
that map and may reword a shipped gloss; it is **not** a whitelist — `signal add` and the
coach may both use a category outside it, and a new one is flagged for confirmation rather
than refused. That preserves `DESIGN_signal_authoring.md` §1: `metric` stays opaque free
text, and no code branches on a category name (bar the §4 substring).

Shipping a default table that config overrides is the existing house pattern —
`intensity.COVERAGE_MIN_BY_SPORT` under `garmin.zone_coverage_display_min_by_sport` does
exactly this, and is a stronger domain commitment than a prompt vocabulary.

**Situational categories stay in config**, not in the shipped list: `altitude`,
`medication`, `menstrual_cycle` apply to some athletes and would otherwise be suggested to
every one. They ship as commented examples in `config_template_full.yaml`. There is
deliberately no removal syntax — nothing shipped needs removing. If a situational category
ever enters the shipped set, `metric: null` meaning *drop this one* becomes necessary.

**What the rendered list is really teaching** is *granularity*, not spelling. Each line
carries the category's day-count:

```
- alcohol (23 days, last 2026-08-24): alcohol drunk the evening before; value = number of
  standard drinks ...
- stress (not yet used): life stress outside training — work, family, money, a bad week.
```

`alcohol — 23 days` makes it self-evident that a category recurs. Without that, a model
asked to file "kid was ill, up half the night" writes `child_illness_disrupted_sleep` —
not a misspelling of anything, but a description masquerading as a category, which can
never cluster with anything.

**The gloss** does work the day-counts cannot. It pins what `value` means per category, so
the model has a unit to fill rather than a severity scale to invent (§3), and it
disambiguates categories that read fine but aren't — `illness` says *your own illness, not
a family member's*, which is what stops "kid was ill" landing there.

**Cold start** needs no special case: a fresh instance renders the shipped list with every
row marked `not yet used`.

---

## 6. Near-misses — warn, never auto-merge

When the model coins a category close to an existing one, the athlete gets a **ladder of
two y/N questions**, the existing category first:

```
Log signal: heat = 38 on 2026-08-29..2026-09-02?     [y/N]  -> n
  Log as NEW category 'heatwave' = 38 on ...?        [y/N]
```

Reuse-first on purpose: drift is the failure being prevented, so the question that gets a
reflexive `y` points at the safe outcome, and coining a category takes a deliberate second
answer. Declining both logs nothing, which gives the full three-way outcome out of two y/N
calls — no widening of `runtime.prompt.confirm`, which the Telegram frontend also
implements. Capped at one candidate: a second near-miss should be declined and typed by
hand.

**Detection never rewrites the stored value.** Auto-mapping `sleep_debt` onto `sleep`
would not merely rename it — it would pull the row into the §4 exclusion and change which
channels the analysis may correlate against, on the strength of a string-similarity score.

**Cutoff: 0.6.** Calibrated, not guessed. The drift spellings worth catching sit at 0.667
and up (`heatwave`/`heat`); the closest pair of genuinely distinct shipped categories is
`stress`/`travel` at 0.500. `tests/test_signal_extraction.py` pins both ends, so a category
added to the shipped set cannot quietly become confusable with an existing one. A false
offer costs one extra `[y/N]`; a miss costs a silently fragmented category — so it errs
low. (An initial 0.8 was wrong: it missed `heatwave`/`heat`, the case the feature exists
for.)

---

## 7. Code touch points

- **`trainmate/signals.py`** (new) — `DEFAULT_SIGNAL_METRICS`, `SIGNAL_CHANNEL_EXCLUSIONS`
  + `excluded_channels()`, `normalize_metric`, `format_vocabulary`, `nearest_known`, and
  the shared writer `write_signal_days` (with `date_range`/`signal_summary` lifted out of
  `cli/signals.py`, which now calls them).
- **`config.py`** — `signal_metrics`, merging the shipped map with `coach.signal_metrics`.
- **`coach/service/analysis.py`** — exclusion table moved out; calls `excluded_channels`.
- **`coach/engine/workouts.py`** — `_signal_extraction_task()` (the vocabulary and the
  rules), the `new_signals` schema member, and two new parameters. `has_message` now gates
  six regions, not four; `tests/test_prompt_gates.py` covers the two new ones.
- **`coach/service/planning.py`** — `capture_message_signal`, `known_signal_metrics`.
- **`coach/service/adaptation.py`** — renders the vocabulary, passes the backdate floor,
  reads `new_signals` off the decision onto the proposal.
- **`coach/proposals.py`** — `RevisionProposal.new_signals`.
- **`cli/workouts/generate.py`** — `_confirm_new_signals`, the step-1b ladder.
- **`cli/signals.py`** — shares the writer; warns when `signal add` names an unlisted
  category.

## 8. Resolved decisions

1. **Backdating** → allowed, and needed: signals point at the recent past ("last night").
   Floored at the adapt window's start (`metrics_lookback_days`) so a garbled date cannot
   drop a row into last year.
2. **`value` when the note gives no number** → NULL, never a severity score (§3).
3. **Vocabulary** → suggested, never enforced (§5).
4. **Near-miss** → ladder of two y/N, reuse offered first; no auto-merge (§6).
5. **Sleep category name** → `disturbed_sleep`; `insomnia` rejected for §4.
6. **Positive signals** (sauna, massage) → deliberately not shipped. `_signal_days` is
   sign-agnostic and would accept them, but they are low-magnitude and high-frequency, and
   would dilute the episode analysis.
7. **Injury** → stays a constraint. The coach plans *around* an injury; putting it in both
   places would give one fact two meanings.
