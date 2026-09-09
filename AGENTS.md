# Communication
- Use plain language, short sentences, one idea per sentence.
- Be explicit. Do not imply things. Explain them clearly first.
- Avoid dense or overly compressed phrasing. The problem is never vocabulary, it is
  compression: three ideas stacked in one clause, a mechanism named instead of shown.
- Codebase-private terms ("fingerprint", "the newest live revision", "§5.3") mean nothing
  without the design doc open. Say what the thing is in the same sentence, the first time.
- Explain a behaviour as a story with a real week: "It is Wednesday. Thursday has a ride.
  The coach drops it. Here is what the calendar shows on Thursday afterwards."
- Test every sentence with "does this need the doc or the code open to follow?"
- A **plan** is the periodization: the macrocycle and its mesocycles. Sessions are
  "sessions", "workouts" or "the schedule", never "the plan".
- Never say "regeneration" or "generate" bare. Say `plan generate` (writes the
  periodization) or `workout generate` (writes the sessions). They are a chain, so an
  ambiguous word makes a finding about one read as a finding about the other.

# Design
- Avoid over-engineering. It creeps in through the loop of writing a DESIGN_*.md and then
  reviewing it several times: each pass adds a mechanism for an edge case. Before adding
  one, ask whether a plausible athlete hits that case in a real week; if not, leave it out
  with a one-line "not handled". Treat a review pass as a chance to remove as much as to
  add; a pass that only adds is suspect.
- Design for athletes in general, not for the author's own routine or equipment. State the
  general rule first and use the author's data only as a worked example.
- Never infer a fact from noisy data (Garmin, a partial template match). Ask the human, or
  record it only from an authoritative source.
- In companion (simple) mode, a difference between two paths that cannot be explained in one
  sentence is a design bug. Collapse the behaviours; do not keep the affordance and write
  better copy for it. Expert mode is exempt.
- A destructive command defaults to the reversible action (archive) and keeps the hard
  cascade behind `--purge`. If the docs warn the reader off a default every time it is
  mentioned, the default is wrong; fix it rather than repeat the caveat.

# Code
- Read docs/ARCHITECTURE.md to ramp up on the code structure.
- When changing code, always update docs/ARCHITECTURE.md if appropriate.
- When adding a new feature, always reflect if this needs to be integrated
  in each command and data flow.
- When storing values in the database, always lean toward storing exact or
  very precise values, unless there are real savings into reducing the
  precision. Only round the values on display.
- Avoid duplication. Do not duplicate function with business logic, instead
  re-use the existing code if this doesn't add too much complexity.
- Every TrainMate instance is operated by the author (the companion instance runs a second
  athlete from the same checkout). So when a migration is necessary, plan for a one-off
  migration and don't bloat the code with backward compatibility support.

# Git policy
- Never push anything to the remote without asking before.
- Never commit without being explicitly asked by the user.
- Never open a pull request.
- Land a worktree branch by rebasing onto main and fast-forwarding. Only a branch with
  enough commits to be worth a node in the graph earns `--no-ff`.
- User review: summarize the changes that were made.
- Commit message format:

  """
  <file>: One line summary of the change, less than 80-100 characters if possible.

  Longer description and details. The lines should be less than 80-100
  characters.
  """

  "<file>" is present only if the change mostly affects a single file, in that
  case, use the basename of the filename.

# Code style
- Everything wraps at 100 characters: code lines, comments and LLM prompts.
- Multiline LLM prompts must use string literal enclosed in triple quotes
  or implicit string concatenation with parenthesis.
- LLM prompts have one section hierarchy, in both the system and the user
  message: `## SECTION NAME` for a top-level section, `### SUB-SECTION NAME`
  for a sub-section of `## TASK`, and a `====` banner only around a document
  quoted verbatim. Names stay ALL CAPS, no trailing colon. See
  designs/DESIGN_prompt_structure.md before adding a section (every design doc lives
  under designs/).
- Prefer early exits — `continue`, `return`, `raise` — over nesting a block
  inside a conditional. This is about removing *indentation levels*, not about
  removing *lines*: never compress a branch into a conditional expression to
  save one. Two plain four-line branches beat one dense ternary, and a ternary
  that decides two things at once (which list, which value) is always worse
  than the `if`/`else` it replaced.
  For instance, instead of:

    for filename in file_list:
        if filename.endswith(".md"):
            try:
                do_something(filename)
            except Exception as e:
                print(f"Error reading science guideline {filename}: {e}")

  Use:

    for filename in file_list:
        if not filename.endswith(".md"):
            continue
        try:
            do_something(filename)
        except Exception as e:
            print(f"Error reading science guideline {filename}: {e}")
- When displaying prose from the LLM, always run it through `wrap_text` in
  `trainmate/util.py`, so the words wrap nicely.
- One command family per file under `trainmate/cli/`, and split a family into
  a file per command once it outgrows roughly 400 lines. `plans.py`, `render.py`, `bot.py`
  and `progress.py` are over the limit; they are debt, not precedent. Code shared by two
  commands goes in a module of its own — `cli/common.py` for renderers, otherwise its own
  file — never in whichever command file happened to define it first. A function-local
  import added to dodge a cycle between two CLI modules is the signal that shared code is
  in the wrong place; move it rather than deferring the import.
- Comments and docstrings say what the code does and name the design section
  that says why (`DESIGN_x.md §N`). Do not restate the rationale: it is already
  written down once, and a paraphrase beside the code is the copy that goes
  stale. One line of "why" plus the pointer is the target. The same caveat never
  appears in two files: pick its one home and point at it from elsewhere.

# Test
- Use "unittest" module.
- Run tests using: `venv/bin/python -m unittest discover -s tests -p "test_*.py"`
- You can run the tests without asking the user.
- A fresh worktree fails at test collection until `config.yaml`, `service_account.json`
  and `venv` are symlinked in from the main checkout. Do not symlink `trainmate.db`: the
  worktree's own empty database is what keeps a stray run off the athlete's data.
- An invariant that spans files ("a propose method never writes") gets a test that reads
  the source and names the offender. Key it on a shape (a return annotation, a directory
  glob), never on a hand-maintained list of names.
- Before writing that test, try to delete the invariant instead: collapsing two commands
  into one flow removes the invariant, the test and the drift together.
