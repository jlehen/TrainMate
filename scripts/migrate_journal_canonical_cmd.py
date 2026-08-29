#!/usr/bin/env python3
"""One-off migration: name the runs already in the journal (DESIGN_logging.md §7.1).

Run once, by hand, against the live journal:

    venv/bin/python scripts/migrate_journal_canonical_cmd.py --yes

`run.end` now carries `cmd`, the canonical command the parse resolved to, because
`run.start` records the argv as it was typed and the athlete types prefixes — `j`,
`wo li`. The listing filters, groups and matches on that name; a record written before
the field existed has none, so its run is listed rather than hidden, which is the right
default for something unclassifiable and the wrong answer for a `j` that plainly means
`journal`.

This fills the field in for those older records, by resolving each run's stored argv
exactly the way the dispatcher would: `translate_dashless_argv` first, so a prefix or an
alias becomes its canonical name, then a walk down the real parser tree, so only tokens
that are genuinely sub-commands are taken (`journal ab12` is `journal`, not
`journal ab12`). Nothing else in the file is touched — same lines, same order, one key
added to one object per run.

Idempotent: a `run.end` that already carries `cmd` is left alone, so a second run
reports nothing to do.
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trainmate_cli
from trainmate import journal
from trainmate.cli.argparse_ext import _subparsers_action, translate_dashless_argv


def canonical(parser, argv) -> str:
    """The command an argv resolved to, as `_dispatch` would have resolved it.

    Empty when the line named no command at all (a bare `tm`, an ambiguous prefix the
    parser would have refused): the caller leaves those unnamed rather than guessing."""
    try:
        # Both halves talk to the athlete on the way out; this is a migration, and an
        # "Ambiguous command 'p'" from three days ago is not news.
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.redirect_stderr(io.StringIO()):
                tokens = translate_dashless_argv(parser, [str(a) for a in argv])
    except SystemExit:
        return ""
    words, level = [], parser
    for token in tokens:
        if token.startswith("-") or len(words) == 2:
            break
        action = _subparsers_action(level)
        if action is None or token not in action.canonical_names:
            break
        words.append(token)
        level = action.choices[token]
    return " ".join(words)


def day_files(runs_dir: str) -> list:
    """Every journal day file, oldest first. The same shape the reader accepts, so a
    backup directory beside this one is never picked up (§10)."""
    if not os.path.isdir(runs_dir):
        return []
    return sorted(
        os.path.join(runs_dir, name) for name in os.listdir(runs_dir)
        if journal._DAY_FILE.match(name)
    )


def back_up(runs_dir: str) -> str:
    """Copy the whole directory and verify the copy has every line, before anything else."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{runs_dir.rstrip('/')}.pre-cmd-{stamp}"
    shutil.copytree(runs_dir, backup)
    for original in day_files(runs_dir):
        copy = os.path.join(backup, os.path.basename(original))
        with open(original, "rb") as a, open(copy, "rb") as b:
            if a.read() != b.read():
                raise RuntimeError(f"backup of {original} does not match the original")
    return backup


def read_lines(path: str) -> list:
    with open(path, encoding="utf-8") as handle:
        return [line for line in handle.read().split("\n") if line]


def migrate_file(parser, path: str, dry_run: bool) -> Counter:
    """Fill in `cmd` on every unnamed `run.end` in one day file.

    Rewrites through a temporary file and `os.replace`, so a crash leaves the original
    intact. Refuses if the file grew while it was being read — the journal is appended to
    by whatever else is running, and a rewrite would drop those lines."""
    before = os.stat(path)
    lines = read_lines(path)
    argv_of = {}
    for line in lines:
        rec = journal.parse_record(line)
        if rec and rec.get("ev") == "run.start":
            argv_of[str(rec.get("run"))] = (rec.get("d") or {}).get("argv") or []

    tally, out = Counter(), []
    for line in lines:
        rec = journal.parse_record(line)
        d = rec.get("d") or {} if rec else {}
        if not rec or rec.get("ev") != "run.end" or "cmd" in d:
            out.append(line)
            continue
        name = canonical(parser, argv_of.get(str(rec.get("run")), []))
        if not name:
            tally["unnamed"] += 1
            out.append(line)
            continue
        d["cmd"] = name
        rec["d"] = d
        tally[name] += 1
        out.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))

    if dry_run or not tally:
        return tally
    after = os.stat(path)
    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise RuntimeError(f"{path} changed while it was being read — re-run the script")
    tmp = path + ".migrating"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write("\n".join(out) + "\n")
    shutil.copystat(path, tmp)
    os.replace(tmp, path)
    return tally


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-y", "--yes", action="store_true",
                        help="Skip the confirmation prompt")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Say what would change, write nothing, take no backup")
    parser.add_argument("--dir", default=None,
                        help="Journal directory (default: <logging.dir>/runs)")
    args = parser.parse_args()

    runs_dir = args.dir or journal.runs_dir()
    files = day_files(runs_dir)
    if not files:
        print(f"No journal day files under {runs_dir}.")
        return 1

    # Counted by resolving rather than by looking for a missing key: a bare `tm` named no
    # command and never will, so leaving it out is what makes a second run a no-op.
    cli_parser, _named = trainmate_cli.build_parser()
    tally = Counter()
    for path in files:
        tally += migrate_file(cli_parser, path, dry_run=True)
    pending = sum(count for name, count in tally.items() if name != "unnamed")
    if not pending:
        print(f"Nothing to do: every run in {runs_dir} that names a command is named.")
        if tally["unnamed"]:
            print(f"({tally['unnamed']} run(s) name no command at all — a bare `tm`.)")
        return 0

    print(f"{pending} run(s) with no canonical command, across {len(files)} day file(s).")
    if not args.yes and not args.dry_run:
        answer = input(f"Name them in {runs_dir}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1

    if not args.dry_run:
        backup = back_up(runs_dir)
        print(f"Backed up to {backup}")
        tally = Counter()
        for path in files:
            tally += migrate_file(cli_parser, path, dry_run=False)

    verb = "Would name" if args.dry_run else "Named"
    print(f"{verb} {pending} run(s):")
    for name, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
        if name == "unnamed":
            continue
        print(f"  {count:>3}  {name}")
    if tally["unnamed"]:
        print(f"  {tally['unnamed']:>3}  (left unnamed: the line named no command)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
