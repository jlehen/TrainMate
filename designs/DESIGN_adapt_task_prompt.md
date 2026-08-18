# The `workout adapt` TASK section

## 1. The problem

`coach/engine/workouts.py::_workout_adapt_logic` assembles the adapt prompt's TASK from one
always-on body plus five conditional sections (drift, planned zones, terminal window,
athlete's note, and the note's schema member). Each section was written on its own, at its
own time, against its own DESIGN doc — and each one independently re-derived the same few
house rules before getting to what it actually had to say.

Measured on a real logged run with the drift and terminal-window gates open and no athlete
note. No figures are recorded here: the prompt is edited often, `logs/` is gitignored so an
old measurement cannot be re-derived or checked, and a stale number gets quoted as fact.
Re-measure by rendering the TASK and sizing it per section — that is what turned up the
finding below, and an earlier impression of which sections were heaviest proved wrong.

Five ideas were each stated in several separate places, in full, as if for the first time:

| idea | restated in |
| --- | --- |
| prefer rescheduling over deleting/cutting | `ATTRIBUTING`, athlete-added, terminal window, benchmark |
| do not reshape / re-cut the mesocycle | `ATTRIBUTING`, note (twice), drift, terminal window |
| name the cause in `change_reason` | athlete-added, drift, note — and the schema field itself |
| recovery metrics lag | `ATTRIBUTING`, `DO NOT COMPOUND` |
| informational and removed sessions are not misses | task header, removed-sessions paragraph |

The `ATHLETE'S NOTE FOR TODAY` section stated "do not reshape the mesocycle" **twice within
itself**, a few lines apart.

Two things follow from restating a rule rather than referring to it. The prompt pays for it
every run — but more importantly, the copies drift: each site phrases the rule against its own
local concern, so "prefer rescheduling" in the terminal window and "prefer rescheduling" for
an athlete-added session slowly stop meaning quite the same thing, and nothing catches it.

## 2. Standing rules

The TASK now states the shared rules once, immediately after the decision branches, under a
heading that says what they are:

```
STANDING RULES — these govern every section below, and it does not restate them:
1. MOVE BEFORE YOU EASE, EASE BEFORE YOU DELETE.
2. THE BLOCK IS NOT YOURS TO RESHAPE.
3. NAME THE CAUSE.
4. NOT EVERY GAP IS A MISS.
5. RECOVERY METRICS LAG.
```

Placement is deliberate: **after** the three (or four) branches, not before. The branches are
the decision the model is here to make; the rules bound how it may express that decision. Put
first, they read as the task itself.

Each rule earns its place by having been load-bearing in more than one section:

- **Rule 1** is the single most repeated instruction in the old prompt, and the one most at
  risk of quiet divergence. It is a preference order, not a prohibition — every section that
  used to restate it wanted exactly the same order.
- **Rule 2** is the firewall `DESIGN_block_boundary.md` §2 and
  `DESIGN_intensity_distribution.md` §9.2 both describe from their own side. It is what
  "adapt is tactical and within-block" means operationally. The rule names the three signals
  that were each separately declared insufficient to re-cut a block — a depressed morning, a
  note, a drift reading — so a fourth signal added later inherits the rule instead of needing
  its own copy of it.
- **Rule 3** was stated in three sections *and* in the `change_reason` schema field itself,
  which is where a field-level instruction belongs. The rule is the general form; the schema
  field keeps the specifics.
- **Rule 4** merges two paragraphs that said the same thing about different data — a session
  the plan never governed, and a session the athlete deliberately removed. Neither is an
  adherence failure; both still count toward load and intent.
- **Rule 5** is the mechanism behind two otherwise-unrelated sections: it is why
  `ATTRIBUTING A DEPRESSED MORNING` reads yesterday's context signal, and why
  `DO NOT COMPOUND A PRIOR ADAPTATION` exists at all. Stated once, both sections cite it in a
  clause ("By rule 5…", "Rule 5 again:") instead of re-explaining lag from scratch.

**A rule does not displace a mechanic.** Where a section needs a *procedure* rather than a
preference, it keeps it in full: `PROTECTING A BENCHMARK` still spells out that moving a test
means emitting it on its new date plus a replacement on the old one, because rule 1 tells the
model to prefer moving and says nothing about how to encode one. Likewise the drift section
keeps its escalation to `workout generate` (§9.2a's handoff) and the terminal window keeps
"do not deepen a cut to carry the athlete into the next block" — both are specific
consequences, not restatements.

## 3. Result

Every idea in §1's table is now stated once, as a standing rule. The sections that used to
restate it either cite it in a clause or say nothing. Two second statements survive, both
mechanics per §2: how to encode a benchmark move, and the drift section's escalation to
`workout generate`.

The prose shrank by well under a fifth, and it is worth being plain about why the saving is
modest: the redundancy was never where most of the tokens were. The response schema and
`PRESCRIBING INTENSITY` are the two largest sections and neither is redundant — the schema is
a contract with the parser, and `PRESCRIBING INTENSITY` is shared verbatim with
`workout generate` (`_planned_zone_task`), so it is out of scope here by construction.

**The TASK is not where the adapt prompt's weight is at all.** The science guidelines
dominate the prompt, and much of what they carry is material `adapt` has no authority to act
on. Slimming those — a per-command guideline set in
`coach/formatting.py::_load_science_guidelines` — is worth several times this change, for a
smaller edit. Measure it before starting rather than trusting any figure recorded here.

## 4. Where the rationale went

Prompt prose that explained *why* a rule holds was removed in favour of the instruction
alone. The reasoning was not deleted — in every case it already existed in a DESIGN doc, or
now does:

| removed from the prompt | lives in |
| --- | --- |
| why a test may never be softened, why moving it is the model's call | `DESIGN_benchmark_workouts.md` §4 |
| why an easing at a block's end cannot rebound | `DESIGN_block_boundary.md` §1, §3 |
| why adapt may sharpen a prescription but not re-shape a block | `DESIGN_intensity_distribution.md` §9.2, §9.2a |
| why drift up and drift down are the same correction mirrored | `DESIGN_intensity_distribution.md` §9.4 |
| why the standing rules exist and what each replaced | this document, §2 |

This is the same rule the codebase applies to comments: the instruction goes where it acts,
the reasoning goes in the design doc, and neither is duplicated across files.

## 5. Deliberately not done

- **Rewriting `PRESCRIBING INTENSITY`.** `_planned_zone_task` is shared with
  `workout generate`; compressing it changes two prompts and belongs with a review of the
  generate TASK, not this one.
- **Turning prose rules into schema/code.** Several sections still *ask* the model not to do
  what the app could *refuse* — the clearest case is the benchmark move, which could be a
  `moved_from` field with the delete-and-reinsert done in code, retiring most of
  `PROTECTING A BENCHMARK`. That is a behaviour change with a proposal-path and apply-path
  cost; this rewrite is text-only and deliberately load-neutral.
- **Trimming the response schema.** It is a contract with the parser. Shortening it trades
  tokens for malformed responses.
- **Touching the user-content preamble.** The "Planned Workouts" framing is verbose, but it
  is data framing rather than instruction, and it is read against the data it introduces.

## 6. What holds this in place

`tests/test_prompt_gates.py` pins the conditional regions: each gated feature must appear in
every place it belongs or in none, and the schema must stay well-formed across the gated
member. `tests/test_adaptation_adapt.py` pins the section headers and the phrases the
handoffs depend on — `THIS BLOCK IS ENDING` with its rendered day count,
`CORRECTING EXECUTION DRIFT:`, the drift branch's "even when recovery metrics are fine", and
the escalation "belongs to the next `workout generate`". A rewrite that dissolves one of
those into a standing rule would pass a reading and fail the suite, which is the point: the
escalation in particular must stay inside the gated drift section, not float up into an
always-on rule, or the test would no longer be proving it is gated.
