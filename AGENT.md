# VCS policy
- No automatic commits.
- User review: always show the diff to the user and explain the changes that
  it contains.
- Commit message format:

  """
  <file>: One line summary of the change, less than 80-100 characters if possible.

  Longer description and details. The lines should be less than 80-100
  characters.
  """

  "<file>" is present only if the change mostly affects a single file, in that
  case, use the basename of the filename.  Otherwise, don't just it.

  Example: [coach] Improve prompt.

# Code style
- Code lines should not be longer than 100 characters.
- Comments should be wrapped at 100 characters.
- Try to reduce indented code, unless it's very trivial (1-2 lines). For instance, instead of:

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
