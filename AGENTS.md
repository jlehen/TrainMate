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
- Try to reduce indented code, unless it's very trivial (1-2 lines).
  For instance, instead of:

    for filename in file_list;
        if filename.endswith(".txt"):
          try:
            do_something(filename)
          except Exception as e:
            print(f"Error reading science guideline {filename}: {e}")
  
  Use:

    for filename in file_list;
        if not filename.endswith(".txt"):
          continue
        try:
          do_something(filename)
        except Exception as e:
          print(f"Error reading science guideline {filename}: {e}")

# Test
- Use "unittest" module.
- Run tests using: `venv/bin/python -m unittest discover -s tests -p "test_*.py"`
- You can run the tests without asking the user.
