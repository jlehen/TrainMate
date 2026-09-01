"""The suite-wide isolation guards must actually fire.

Both guards installed in tests/__init__.py are backstops for seams that fail silently:
a handle rebinding that misses a site would otherwise read the athlete's real training
data, and a live network call would hit Garmin/OpenRouter for real. A guard that stopped
working would be invisible, so it is asserted here rather than trusted.
"""
import os
import socket
import sqlite3
import tempfile
import unittest

PRODUCTION_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trainmate.db")

_UNSET = object()   # "nothing was bound", as opposed to "bound to None"


class TestProductionDatabaseIsUnreachable(unittest.TestCase):
    def test_opening_the_real_database_raises(self):
        with self.assertRaises(RuntimeError) as caught:
            sqlite3.connect(PRODUCTION_DB)
        self.assertIn("must not open the production database", str(caught.exception))

    def test_the_relative_path_is_caught_too(self):
        """The guard compares absolute paths, so a relative spelling is the same file."""
        cwd = os.getcwd()
        os.chdir(os.path.dirname(PRODUCTION_DB))
        try:
            with self.assertRaises(RuntimeError):
                sqlite3.connect("trainmate.db")
        finally:
            os.chdir(cwd)

    def test_other_databases_still_open_normally(self):
        path = os.path.join(tempfile.mkdtemp(), "scratch.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (a INTEGER)")
        conn.close()
        self.assertTrue(os.path.exists(path))


class TestSavingTheHandlesDoesNotBuildThem(unittest.TestCase):
    """`helpers.restore_db_handles` is what a module calls before binding its own
    Database. Remembering the handles must not *create* one — reading either of them as
    an attribute resolves a module `__getattr__` that builds the real database against
    the production file, and the guard above then refuses it. Two modules wrote that by
    hand and both hit it, but only when run alone: inside the full suite an earlier
    module had already bound a handle, so the read found one cached.
    """

    def setUp(self):
        from trainmate import runtime
        import trainmate.db
        self.modules = (runtime, trainmate.db)
        # Whatever the suite has bound so far goes back untouched, however this ends.
        saved = [(m, vars(m).get("db", _UNSET)) for m in self.modules]

        def _put_back():
            for module, previous in saved:
                if previous is _UNSET:
                    vars(module).pop("db", None)
                else:
                    module.db = previous
        self.addCleanup(_put_back)
        for module in self.modules:
            vars(module).pop("db", None)

    def test_remembering_an_unbound_handle_builds_nothing(self):
        from tests.helpers import restore_db_handles
        restore_db_handles(unittest.TestCase())      # would raise if it built one
        for module in self.modules:
            self.assertNotIn("db", vars(module), module.__name__)

    def test_an_unbound_handle_is_restored_to_absent(self):
        """Not to some value: leaving a concrete singleton where the lazy accessor was
        hands the next module a stale database instead of one it can still bind."""
        from tests.helpers import restore_db_handles
        borrower = unittest.TestCase()
        restore_db_handles(borrower)
        for module in self.modules:
            module.db = "a handle this test bound"
        borrower.doCleanups()
        for module in self.modules:
            self.assertNotIn("db", vars(module), module.__name__)


class TestNetworkIsUnreachable(unittest.TestCase):
    def test_remote_connections_raise(self):
        with self.assertRaises(RuntimeError) as caught:
            socket.socket().connect(("garmin.example.com", 443))
        self.assertIn("must not reach the network", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
