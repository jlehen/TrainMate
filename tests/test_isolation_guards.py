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


class TestNetworkIsUnreachable(unittest.TestCase):
    def test_remote_connections_raise(self):
        with self.assertRaises(RuntimeError) as caught:
            socket.socket().connect(("garmin.example.com", 443))
        self.assertIn("must not reach the network", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
