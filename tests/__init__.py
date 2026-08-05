"""Test-package init: installs the suite-wide no-network guards.

`unittest discover` imports this package before collecting any test module, so both
guards are in place for every test.
"""

import atexit
import glob
import os
import socket
from unittest import mock


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
# (DESIGN_calendar_context_ingest.md §6). Stub the bridge, not the callers.
mock.patch("trainmate.garmin.sync._sync_calendar_context", lambda *a, **k: None).start()

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
