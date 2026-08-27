"""Test-package init: installs the suite-wide no-network guards.

`unittest discover` imports this package before collecting any test module, so both
guards are in place for every test.
"""

import atexit
import glob
import os
import shutil
import socket
import sqlite3
import tempfile
from unittest import mock


_PRODUCTION_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "trainmate.db")
_real_sqlite_connect = sqlite3.connect


def _guarded_sqlite_connect(database, *args, **kwargs):
    """Backstop for the database seam, mirroring the network guard below.

    A test whose handle-rebinding misses a site falls through to the real training
    database — which, unlike a stray network call, otherwise succeeds silently and
    leaves the test passing while measuring nothing (or worse, writing to it).

    Installed before anything imports the app, so it also covers connections opened at
    import time. That ordering only became possible once the singletons went lazy: the
    package used to build a Database — running the schema migrations against the real
    file — merely because something imported it.
    """
    if os.path.abspath(str(database)) == os.path.abspath(_PRODUCTION_DB):
        raise RuntimeError(
            f"tests must not open the production database ({_PRODUCTION_DB}); "
            "bind an isolated one with tests.helpers.bind_test_db()"
        )
    return _real_sqlite_connect(database, *args, **kwargs)


sqlite3.connect = _guarded_sqlite_connect


@atexit.register
def _remove_test_databases() -> None:
    """Sweep the per-module SQLite files at the very end of the run.

    Each module deletes its own, but a later module reaching a handle that still
    points at the deleted path recreates an empty file there, so the last word has
    to come after every module has finished.
    """
    for path in glob.glob(os.path.join(os.path.dirname(__file__), "*.db")):
        try:
            os.remove(path)
        except OSError:
            pass

# The Calendar ride-along inside ensure_data ran against the real account: it created
# events and consumed the incremental sync token that `data pull` needs
# (DESIGN_calendar_signal_ingest.md §6). Stub the bridge, not the callers.
mock.patch("trainmate.garmin.sync._sync_calendar_signals", lambda *a, **k: None).start()

# Third seam, same idea: every command a test runs opens a journal run
# (DESIGN_logging.md §3), so a suite left pointing at the real logging.dir appends
# several hundred KB of `test` runs to the operator's own journal. Redirect the whole
# directory — the LLM exchange files hang off it too — and sweep it at exit.
from trainmate.config import config as _config  # noqa: E402 -- after the db guard

_TEST_LOG_DIR = tempfile.mkdtemp(prefix="trainmate-test-logs-")
_config.data.setdefault("logging", {})["dir"] = _TEST_LOG_DIR
os.environ.setdefault("TRAINMATE_SOURCE", "test")
atexit.register(shutil.rmtree, _TEST_LOG_DIR, True)

_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}
_real_connect = socket.socket.connect


def _guarded_connect(self, address):
    """Backstop: a test reaching a remote host is a bug, so fail loudly not silently."""
    host = address[0] if isinstance(address, tuple) else address
    if host not in _LOOPBACK:
        raise RuntimeError(
            f"tests must not reach the network (attempted connection to {host!r}); "
            "mock the client instead"
        )
    return _real_connect(self, address)


socket.socket.connect = _guarded_connect
