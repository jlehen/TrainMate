# Prompt structure: one hierarchy, three markers

## 1. The problem

Every prompt TrainMate sends is assembled from parts written at different times, in
different files, by whoever needed the next section. Each part invented its own way of
announcing itself, and the result read — to a human and to the model — as one flat list of
shouting.

Rendered, a `workout generate` system prompt looked like this:

```
You are TrainMate Coach, ...

COACHING ROLE AND OBJECTIVES:
...
================================================================================
START OF SPORTS SCIENCE GUIDELINES
================================================================================
=== Guidelines from benchmarks.md ===
BENCHMARK (FITNESS TEST) GUIDELINES
===================================
...
=== Guidelines from periodization.md ===
# TrainMate Engine Architecture: Periodization & Training Structure Guide
## 1. Core Architectural Overview
...
=== Guidelines from sustainable_training.md ===
...
================================================================================
END OF SPORTS SCIENCE GUIDELINES
================================================================================

ATHLETE PROFILE & PREFERENCES:
...
TASK:
Generate a training schedule for the next 53 days ...

BENCHMARK PLACEMENT (fitness tests — see the BENCHMARK guidelines above):
...
CONTINUING A BLOCK ALREADY UNDER WAY:
...
```

Three problems, in increasing order of how much they cost:

1. **No hierarchy.** `TASK:` and `BENCHMARK PLACEMENT:` are the same shape, so nothing on
   the page says the second is *part of* the first. They are: everything from `TASK:` to the
   response schema is one `custom_task` string. The model has to infer nesting from meaning
   alone, and a reader has to do the same work.
2. **One banner over two authorities.** TrainMate's own science files and the athlete's
   supplied material were concatenated inside a single `START OF SPORTS SCIENCE
   GUIDELINES` banner, distinguishable only by a filename the model has no reason to read
   as provenance. Whose claim is whose became guesswork.
3. **Three conventions for the same job.** The system prompt announced sections in
   `ALL CAPS:`, the user content in `Title Case:` (`Athlete's Metrics History (Past 15
   Days):`, `Baseline Reference:`) — except for three that were caps
   (`ATHLETE'S NOTE FOR THIS ADAPTATION`, `BLOCK PROGRESS SO FAR`) — and the science
   documents in whatever markdown their author happened to use. Meanwhile task text refers
   to sections *by name* ("the section titled `BLOCK PROGRESS SO FAR`", "see `PROTECTING A
   BENCHMARK`"), so those names are load-bearing and their shape should be predictable.

## 2. The scheme

Three markers, one meaning each. Every prompt in the app — system message and user message
alike, for all four commands — uses these and nothing else.

| marker | means | example |
| --- | --- | --- |
| `## NAME` | a top-level section of the prompt | `## TASK`, `## ATHLETE PROFILE & PREFERENCES`, `## BASELINE REFERENCE` |
| `### NAME` | a sub-section, and only ever inside `## TASK` | `### BENCHMARK PLACEMENT`, `### STANDING RULES` |
| `====` banner | a document quoted verbatim, not written here | the science guidelines (§3) |

Three rules go with them:

- **Section names stay ALL CAPS.** The prose refers to sections by name, so a name must be
  recognizable as a name wherever it appears. The marker carries the level; the caps carry
  the identity.
- **No trailing colon.** The marker already announces a heading. `## TASK:` says it twice.
- **Both messages use the same scheme.** The user content is not a lesser document — it
  holds the athlete's note, the block progress, the planned workouts. `## BLOCK PROGRESS SO
  FAR` in the user message and "`the section titled "BLOCK PROGRESS SO FAR"`" in the system
  message now agree on more than the words.

A parenthetical gloss may ride on the heading line where it says what the section *is*
(`## ACTIVE CONSTRAINTS (athlete-declared directives to work around)`). Anything that does
not fit the project's 100-character wrap is body text, on its own line below the heading —
`## PLANNED WORKOUTS` used to carry three paragraphs of instruction inside its heading, and
`## ATHLETE-SPECIFIC OBSERVATIONS` carried a line break, which is not a heading at all.

The opening line of each message stays unheaded: the system prompt's role statement and the
user message's request are the lede, not a section.

## 3. Why the science documents keep a banner

Markdown headings would be the obvious single convention for everything — except that the
science files *are* markdown, with their own `#`, `##` and `###` at whatever depth their
author chose. `periodization.md` opens with a `#` title and `## 1. Core Architectural
Overview`. Under a markdown-only scheme, that heading and `## TASK` would be siblings, and
a quoted document's internal structure would read as the prompt's.

So the banner is not decoration and not legacy: it is the marker that says **what follows
is quoted, and its headings are its own**. It is the reason the frame can safely use `##`
everywhere else.

Two banners, not one — one per source, each stating its provenance:

```
================================================================================
START OF TRAINMATE SPORTS SCIENCE GUIDELINES
================================================================================
TrainMate's own reference material, shipped with the app.

--- benchmarks.md ---
...
================================================================================
END OF TRAINMATE SPORTS SCIENCE GUIDELINES
================================================================================

================================================================================
START OF ATHLETE-PROVIDED SPORTS SCIENCE GUIDELINES
================================================================================
Reference material the athlete supplied themselves — the training philosophy
and sources they want their coaching drawn from.

--- sustainable_training.md ---
...
```

The per-file marker drops from `=== Guidelines from X.md ===` to `--- X.md ---`: inside a
banner, the `=` rule is taken, and the lighter marker keeps the nesting visible.

`formatting._load_science_guidelines` emits the banners itself rather than returning bare
text for each caller to wrap. Three call sites used to hand-write the same banner literal
around it; now the block that must not be misread carries its own frame, and a directory
with no documents produces no banner at all instead of an empty one.

**Open question — precedence.** Splitting the banners tells the coach *whose* material each
claim comes from. It deliberately does not say what to do when the two disagree, because
nothing in the app has ever said. If a precedence rule is wanted ("the athlete's material
governs where it is specific; TrainMate's where it is silent", or the reverse), it belongs
in the provenance line, and it is a coaching decision rather than a formatting one.

## 4. What is a sub-section, concretely

Everything a command appends to `custom_task` is under `## TASK` — that is what the string
is. So the conditional sections are `###`:

| command | `### sub-sections of TASK` |
| --- | --- |
| `plan generate` | `ATHLETE FEEDBACK ON THE PREVIOUS PLAN`, `CONTINUITY WITH THE PREVIOUS PLAN` |
| `workout generate` | `BENCHMARK PLACEMENT`, `CONTINUING A BLOCK ALREADY UNDER WAY`, `JUDGING THE BLOCK'S COMPOSITION`, `PRESCRIBING INTENSITY` |
| `workout adapt` | `STANDING RULES`, `WHAT YOU MAY NOT TOUCH`, `ATTRIBUTING A DEPRESSED MORNING`, `DO NOT COMPOUND A PRIOR ADAPTATION`, `PROTECTING A BENCHMARK`, `CORRECTING EXECUTION DRIFT`, `PRESCRIBING INTENSITY`, `THIS BLOCK IS ENDING`, `ATHLETE'S NOTE FOR TODAY`, `EXTRACTING A DURABLE CONSTRAINT FROM THE NOTE`, `DURABLE OBSERVATIONS ARE READ-ONLY HERE` |
| `data analyze` | `READING THE PER-WEEK CONTEXT FIELDS`, `READING 'context_days'` |

Two sections gained a heading they never had, because a `###` scheme has no place for an
unlabelled trailing paragraph: adapt's read-only-learnings note
(`### DURABLE OBSERVATIONS ARE READ-ONLY HERE`) and planning's previous-strategy paragraph
(`### CONTINUITY WITH THE PREVIOUS PLAN`).

The response schema is **not** a sub-section. "You MUST respond with a JSON object
containing" is a peer of the task, not part of it, so it is `## RESPONSE FORMAT` in all four
commands. Previously it was an unheaded paragraph trailing whichever section happened to
come last — which differed per command and per gate.

## 5. The log file

`openrouter._log_exchange` writes each exchange to `logs/llm_exchanges/` as markdown, with
`## System Prompt` / `## User Content` as its own structure. The prompt inside now carries
`##` markers of its own, so both message bodies are fenced. Unfenced, a prompt section
would render as a heading of the log file and the two hierarchies would interleave into
one.

Worth stating plainly because it caused the confusion that prompted this doc: **`##
System Prompt` and `## User Content` are the log's headings, not the prompt's.** The model
never sees either — they are two separate API messages, and the log is only how a human
reads them side by side.

## 6. What this does not change

No instruction, rule, gate, or schema field changed wording. The gates that must move
together still do (`tests/test_prompt_gates.py` asserts it) and the section *names* — the
referents the prose cites — are all preserved, so every "see `PROTECTING A BENCHMARK`"
still resolves. What changed is the markers around them and, for the science documents,
which banner they sit under.
