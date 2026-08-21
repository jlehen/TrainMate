"""Structural invariants on `CoachService`, checked against the source rather than trusted.

The proposal/apply split is stated in `coach/proposals.py` and in every propose method's
docstring, and prose did not stop two commands from breaking it: `workout_adapt` grew a
`mark_honored` write on its no-change branch, and a preview re-read the database for rows
its proposal already carried. These tests turn both rules into something that fails.
"""
import ast
import pathlib
import unittest

SERVICE_DIR = pathlib.Path(__file__).resolve().parents[1] / "trainmate/coach/service"
CLI_DIR = pathlib.Path(__file__).resolve().parents[1] / "trainmate/cli"

# A `db` method starting with one of these writes. Readers are `get_*`/`list_*`/`count_*`.
WRITE_PREFIXES = (
    "save_", "add_", "update_", "delete_", "remove_", "rm_", "set_", "mark_", "clear_",
    "archive_", "restore_", "apply_", "capture_", "wipe_", "insert_", "supersede_",
)


def _functions(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _db_writes(fn: ast.AST):
    """Every `self._db.<write>` / `runtime.db.<write>` called in `fn`'s body."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func
        if not target.attr.startswith(WRITE_PREFIXES):
            continue
        owner = target.value
        if isinstance(owner, ast.Attribute) and owner.attr in ("_db", "db"):
            yield target.attr


class TestProposingNeverWrites(unittest.TestCase):
    """A method that returns a *Proposal* has not been accepted yet, so it must not
    touch the database — the whole point of handing the caller a proposal is that the
    write waits for the athlete's `y` (DESIGN_constraint_reschedule.md §7/§8).

    Keyed on the return annotation, so a new propose method is covered the day it is
    written rather than the day someone remembers to list it here.
    """

    def test_no_propose_method_writes(self):
        offenders = []
        for path in sorted(SERVICE_DIR.glob("*.py")):
            for fn in _functions(path):
                annotation = ast.unparse(fn.returns) if fn.returns else ""
                if "Proposal" not in annotation:
                    continue
                for write in _db_writes(fn):
                    offenders.append(f"{path.name}:{fn.name} calls db.{write}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class TestApplyingAlwaysStamps(unittest.TestCase):
    """The mirror of the rule above, and the half that actually shipped broken: a method
    that TAKES a *Proposal* is acting on the athlete's `y`, so it must record the coach
    pass — `honoring.stamp` — or the sweep re-offers the constraint forever at one LLM
    call a run (DESIGN_constraint_reschedule.md §8).

    Keyed on the parameter annotation, the same way `TestProposingNeverWrites` is keyed on
    the return annotation, so an apply method written tomorrow is covered tomorrow.
    """

    def test_every_proposal_taking_method_records_the_pass(self):
        offenders, checked = [], []
        for path in sorted(SERVICE_DIR.glob("*.py")):
            for fn in _functions(path):
                takes_proposal = any(
                    a.annotation is not None and "Proposal" in ast.unparse(a.annotation)
                    for a in fn.args.args + fn.args.kwonlyargs
                )
                if not takes_proposal:
                    continue
                checked.append(f"{path.name}:{fn.name}")
                calls = {
                    node.func.attr for node in ast.walk(fn)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                }
                if "stamp" not in calls:
                    offenders.append(f"{path.name}:{fn.name} never calls honoring.stamp")
        self.assertEqual(offenders, [], "\n".join(offenders))
        # A rule nothing matches is a rule that has quietly stopped being checked.
        self.assertTrue(checked, "no Proposal-taking service method found at all")


class TestPreviewsRenderTheProposal(unittest.TestCase):
    """The preview must draw what the proposal carries, never re-read the rows behind it.

    `coach/proposals.py` exists because a preview that re-derives its own facts can
    disagree with the apply that follows it — which is exactly how the range bug it
    documents happened.
    """

    def test_no_revision_preview_reads_the_database(self):
        # Keyed on the shape — any `db.<anything>` access under the preview module — not
        # on the one method name that happened to be the bug (AGENTS.md, structural tests).
        offenders = []
        for path in sorted((CLI_DIR / "workouts").glob("revisions*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Attribute):
                    continue
                owner = node.value
                if isinstance(owner, ast.Attribute) and owner.attr in ("_db", "db"):
                    offenders.append(f"{path.name} reads db.{node.attr}")
        self.assertEqual(offenders, [], "\n".join(offenders))
