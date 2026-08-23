#!/usr/bin/env python3
"""Extract per-commit codebase and test metrics from git history into a CSV.

Columns emitted:
- date: Commit date in ISO 8601 format.
- commit id: Full or short commit hash.
- number of code files (trainmate): Total Python files in trainmate/ at that commit.
- total python code lines (trainmate/): Total lines of Python code in trainmate/ at that commit.
- added python code lines (trainmate/): Net Python lines added (insertions - deletions).
- total test code lines (tests/): Total lines of test code in tests/ at that commit.
- added test code lines (tests/): Net test code lines added (insertions - deletions).
- total tests (tests/): Total test method definitions (def test_*) in tests/ at that commit.
- added tests (tests/): Net test methods added (+def test_* minus -def test_*).
"""

import argparse
import csv
import posixpath
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+test_[a-zA-Z0-9_]+\s*\(")
DIFF_TEST_RE = re.compile(r"^(?P<sign>[+-])\s*(?:async\s+)?def\s+test_[a-zA-Z0-9_]+\s*\(")
RENAME_CURLY_RE = re.compile(r"\{[^{}]*=>\s*([^{}]*)\}")


def resolve_target_path(path_spec: str) -> str:
    """Resolve destination path from git diff rename syntax."""
    if "=>" not in path_spec:
        return path_spec
    if "{" in path_spec and "}" in path_spec:
        return posixpath.normpath(RENAME_CURLY_RE.sub(r"\1", path_spec))
    return posixpath.normpath(path_spec.split("=>")[-1].strip())


def get_commit_entries(
    rev_range: str, chronological: bool = True
) -> List[Tuple[str, str]]:
    """Return (commit_sha, iso_date) pairs in the specified revision range."""
    cmd = ["git", "log", "--topo-order"]
    if chronological:
        cmd.append("--reverse")
    cmd.extend(["--format=%H\t%cI", rev_range])
    output = subprocess.check_output(cmd).decode("utf-8", errors="replace")
    entries = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            entries.append((parts[0], parts[1]))
    return entries


def inspect_blob(sha: str, cache: Dict[str, Tuple[int, int]]) -> Tuple[int, int]:
    """Return (line_count, test_count) for a git blob, caching by blob hash."""
    if sha in cache:
        return cache[sha]
    content = subprocess.check_output(["git", "cat-file", "-p", sha]).decode(
        "utf-8", errors="replace"
    )
    lines = content.splitlines()
    n_lines = len(lines)
    n_tests = sum(1 for l in lines if TEST_DEF_RE.match(l))
    cache[sha] = (n_lines, n_tests)
    return n_lines, n_tests


def get_tree_snapshot_metrics(
    commit: str, blob_cache: Dict[str, Tuple[int, int]]
) -> Tuple[int, int, int, int]:
    """Return snapshot totals: (code_files_count, trainmate_lines, test_lines, total_tests)."""
    cmd = ["git", "ls-tree", "-r", commit]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return 0, 0, 0, 0

    code_files_count = 0
    total_trainmate_lines = 0
    total_test_lines = 0
    total_tests = 0

    for item in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = item.split()
        if len(parts) < 4:
            continue
        _, _, blob_sha, path = parts[0], parts[1], parts[2], parts[3]
        if not path.endswith(".py"):
            continue

        if path.startswith("trainmate/"):
            code_files_count += 1
            n_lines, _ = inspect_blob(blob_sha, blob_cache)
            total_trainmate_lines += n_lines
        elif path.startswith("tests/"):
            n_lines, n_tests = inspect_blob(blob_sha, blob_cache)
            total_test_lines += n_lines
            total_tests += n_tests

    return code_files_count, total_trainmate_lines, total_test_lines, total_tests


def get_net_added_code_lines(commit: str) -> Tuple[int, int]:
    """Return net code lines added (insertions - deletions) for trainmate/ and tests/."""
    cmd = ["git", "show", "--numstat", "--format=", commit]
    output = subprocess.check_output(cmd).decode("utf-8", errors="replace")
    trainmate_net = 0
    tests_net = 0

    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_str, del_str, raw_path = parts[0], parts[1], parts[2]
        if not (add_str.isdigit() and del_str.isdigit()):
            continue

        delta = int(add_str) - int(del_str)
        target = resolve_target_path(raw_path)

        if not target.endswith(".py"):
            continue
        if target.startswith("trainmate/"):
            trainmate_net += delta
        elif target.startswith("tests/"):
            tests_net += delta

    return trainmate_net, tests_net


def get_net_added_tests_count(commit: str) -> int:
    """Return net test methods added (+def test_* minus -def test_*) in tests/."""
    cmd = ["git", "show", "-U0", "--format=", commit, "--", "tests/"]
    output = subprocess.check_output(cmd).decode("utf-8", errors="replace")
    net_tests = 0
    for line in output.splitlines():
        match = DIFF_TEST_RE.match(line)
        if not match:
            continue
        if match.group("sign") == "+":
            net_tests += 1
        elif match.group("sign") == "-":
            net_tests -= 1
    return net_tests


def generate_commit_metrics(
    rev_range: str = "HEAD",
    chronological: bool = True,
    short_hash: bool = False,
) -> List[dict]:
    """Generate metric rows for all commits in the given revision range."""
    entries = get_commit_entries(rev_range, chronological=chronological)
    blob_cache: Dict[str, Tuple[int, int]] = {}
    rows = []

    for commit, cdate in entries:
        cid = commit[:7] if short_hash else commit
        files_count, total_tm_lines, total_test_lines, total_tests = (
            get_tree_snapshot_metrics(commit, blob_cache)
        )
        added_tm_lines, added_test_lines = get_net_added_code_lines(commit)
        added_tests = get_net_added_tests_count(commit)

        rows.append({
            "date": cdate,
            "commit id": cid,
            "number of code files (trainmate)": files_count,
            "total python code lines (trainmate/)": total_tm_lines,
            "added python code lines (trainmate/)": added_tm_lines,
            "total test code lines (tests/)": total_test_lines,
            "added test code lines (tests/)": added_test_lines,
            "total tests (tests/)": total_tests,
            "added tests (tests/)": added_tests,
        })

    return rows


def write_csv(rows: List[dict], output_file: Optional[str] = None) -> None:
    """Write metrics rows to CSV file or standard output."""
    fieldnames = [
        "date",
        "commit id",
        "number of code files (trainmate)",
        "total python code lines (trainmate/)",
        "added python code lines (trainmate/)",
        "total test code lines (tests/)",
        "added test code lines (tests/)",
        "total tests (tests/)",
        "added tests (tests/)",
    ]

    out_stream = (
        open(output_file, "w", newline="", encoding="utf-8")
        if output_file
        else sys.stdout
    )
    try:
        writer = csv.DictWriter(out_stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if output_file:
            out_stream.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build CSV of commit stats: code files, added lines, and added tests."
    )
    parser.add_argument(
        "revision",
        nargs="?",
        default="HEAD",
        help="Git revision or range (default: HEAD).",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Path to write output CSV file (default: stdout).",
    )
    parser.add_argument(
        "--short",
        action="store_true",
        help="Use short (7-char) commit hash instead of full 40-char SHA.",
    )
    parser.add_argument(
        "--newest-first",
        action="store_true",
        help="Order commits from newest to oldest (default: chronological).",
    )

    args = parser.parse_args()
    rows = generate_commit_metrics(
        rev_range=args.revision,
        chronological=not args.newest_first,
        short_hash=args.short,
    )
    write_csv(rows, args.output)


if __name__ == "__main__":
    main()
