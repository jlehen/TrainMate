"""The runtime singleton contract.

Two properties are easy to lose by accident and expensive to notice later: importing
the app must not touch the database, and the library must not depend on the CLI entry
point. Both are asserted here rather than left to convention.
"""
import ast
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestImportsAreSideEffectFree(unittest.TestCase):
    def test_importing_the_app_opens_no_database(self):
        """A fresh interpreter that imports the whole app must not connect to SQLite.

        The db package used to build its singleton at import, which ran the schema
        migrations and wrote to the real file — for `--help`, or for a unit test of a
        pure function.
        """
        probe = (
            "import sqlite3\n"
            "opened = []\n"
            "real = sqlite3.connect\n"
            "sqlite3.connect = lambda *a, **k: (opened.append(a[0]), real(*a, **k))[1]\n"
            "import trainmate_cli\n"
            "import trainmate_web\n"
            "print('OPENED:' + repr(opened))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], cwd=REPO, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OPENED:[]", result.stdout, result.stdout)


class TestLibraryDoesNotImportTheCli(unittest.TestCase):
    def test_no_module_under_trainmate_imports_trainmate_cli(self):
        """The CLI is a frontend; the library must not reach up into it.

        This is what forced the `sys.modules.setdefault("trainmate_cli", ...)` alias,
        and it meant a service call could open a terminal conversation on a caller that
        had no terminal.
        """
        offenders = []
        for root, _, files in os.walk(os.path.join(REPO, "trainmate")):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                tree = ast.parse(open(path).read())
                for node in ast.walk(tree):
                    imported = ()
                    if isinstance(node, ast.Import):
                        imported = tuple(a.name for a in node.names)
                    elif isinstance(node, ast.ImportFrom):
                        imported = (node.module or "",)
                    if any(m == "trainmate_cli" for m in imported):
                        offenders.append(f"{os.path.relpath(path, REPO)}:{node.lineno}")

        self.assertEqual(offenders, [], f"library modules importing the CLI: {offenders}")


if __name__ == "__main__":
    unittest.main()
