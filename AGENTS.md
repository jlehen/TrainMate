# Code
- Read ARCHITECTURE.md to ramp up on the code structure.
- When changing code, always update ARCHITECTURE.md if appropriate.
- When adding a new feature, always reflect if this needs to be integrated
  in each command and data flow.
- When storing values in the database, always lean toward storing exact or
  very precise values, unless there are real savings into reducing the
  precision. Only round the values on display.
- Avoid duplication. Do not duplicate function with business logic, instead
  re-use the existing code if this doesn't add too much complexity.
- TrainMate is used by a single user. So when a migration is necessary,
  plan for a one-off migration and don't bloat the code with backward
  compatibility support.

# Git policy
- Never push without asking before.
- Never commits without being explicitly asked by the user.
- Never open a pull request.
- Never push branches remotely.
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
  DESIGN_prompt_structure.md before adding a section.
- Prefer early exits — `continue`, `return`, `raise` — over nesting a block
  inside a conditional. This is about removing *indentation levels*, not about
  removing *lines*: never compress a branch into a conditional expression to
  save one. Two plain four-line branches beat one dense ternary, and a ternary
  that decides two things at once (which list, which value) is always worse
  than the `if`/`else` it replaced.
  For instance, instead of:

    for filename in file_list;
        if filename.endswith(".md"):
          try:
            do_something(filename)
          except Exception as e:
            print(f"Error reading science guideline {filename}: {e}")
  
  Use:

    for filename in file_list;
        if not filename.endswith(".md"):
          continue
        try:
          do_something(filename)
        except Exception as e:
          print(f"Error reading science guideline {filename}: {e}")
- When displaying prose from the LLM, always run it through the formatter,
  so the words wrap nicely.
- One command family per file under `trainmate/cli/`, and split a family into
  a file per command once it outgrows roughly 400 lines. Code shared by two
  commands goes in a module of its own — `cli/common.py` for renderers,
  otherwise its own file — never in whichever command file happened to define
  it first. A function-local import added to dodge a cycle between two CLI
  modules is the signal that shared code is in the wrong place; move it rather
  than deferring the import.
- Comments and docstrings say what the code does and name the design section
  that says why (`DESIGN_x.md §N`). Do not restate the rationale: it is already
  written down once, and a paraphrase beside the code is the copy that goes
  stale. One line of "why" plus the pointer is the target.

# Test
- Use "unittest" module.
- Run tests using: `venv/bin/python -m unittest discover -s tests -p "test_*.py"`
- You can run the tests without asking the user.
- A rule that spans files needs a test that spans files. When an invariant is
  stated in a docstring or a design section — "a propose method never writes",
  "a preview draws only what the proposal carries" — nothing enforces it at the
  place it breaks, because that place is another file, edited by someone who
  never read the docstring. Pin it with a test that reads the source (AST or
  plain text) and names the offender. Key it on a *shape* — a return annotation,
  a directory glob — never on a hand-maintained list of names, so a file written
  tomorrow is covered tomorrow and not whenever someone remembers it.
- Before writing that test, try to delete the invariant instead. A structural
  test is the right answer when the shape is load-bearing; it is not a licence
  to keep a shape that has to be guarded. Two commands onto one flow needed a
  parser-parity test to stay in step — collapsing them to one deleted the
  invariant, the test, and the drift together.
