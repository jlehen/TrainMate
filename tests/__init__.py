"""Test-package init: installs the suite-wide no-network guards.

`unittest discover` imports this package before collecting any test module, so both
guards are in place for every test.
"""

import atexit
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


# Beside the tests rather than under TMPDIR, which is a RAM-backed tmpfs on most Linux
# boxes: the suite opens ~40 WAL databases at once and a machine short on memory fails
# them with "disk I/O error". `dbs-*` is gitignored.
_TEST_DB_DIR = tempfile.mkdtemp(prefix="dbs-", dir=os.path.dirname(__file__))
atexit.register(shutil.rmtree, _TEST_DB_DIR, True)


def test_db_path(name: str) -> str:
    """The path a test module's SQLite file takes, inside this process's own directory.

    Private per process so two concurrent runs cannot delete each other's databases:
    the files used to sit directly in `tests/`, where the exit sweep removed every
    `*.db` it found — including the ones a second run was still using.
    """
    return os.path.join(_TEST_DB_DIR, name)


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
