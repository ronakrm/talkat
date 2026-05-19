"""Shared pytest fixtures.

This module MUST set XDG_* environment variables BEFORE any talkat module is
imported, because talkat.paths resolves its directory constants at import time
from those env vars. We do this at the very top of the file so that even
indirect imports (e.g. via test collection) see the redirected paths.
"""

import atexit
import os
import shutil
import tempfile

# Create a session-wide temp dir for talkat's XDG paths and point env vars at
# subdirectories of it. This MUST happen before any `import talkat.*` in this
# process — the paths module resolves XDG_* env vars at import time and
# caches them as module-level Path constants.
_SESSION_TMP = tempfile.mkdtemp(prefix="talkat-tests-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_SESSION_TMP, "config")
os.environ["XDG_CACHE_HOME"] = os.path.join(_SESSION_TMP, "cache")
os.environ["XDG_DATA_HOME"] = os.path.join(_SESSION_TMP, "data")
os.environ["XDG_RUNTIME_DIR"] = os.path.join(_SESSION_TMP, "runtime")
for _path in (
    os.environ["XDG_CONFIG_HOME"],
    os.environ["XDG_CACHE_HOME"],
    os.environ["XDG_DATA_HOME"],
    os.environ["XDG_RUNTIME_DIR"],
):
    os.makedirs(_path, exist_ok=True)


@atexit.register
def _cleanup_session_tmp() -> None:
    shutil.rmtree(_SESSION_TMP, ignore_errors=True)


import pytest  # noqa: E402


@pytest.fixture
def clean_config_file():
    """Remove the talkat config file before and after the test."""
    from talkat.paths import CONFIG_FILE

    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
    yield CONFIG_FILE
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()


@pytest.fixture
def clean_pid_files():
    """Ensure PID/lock dirs exist and are empty before and after the test."""
    from talkat.paths import LOCK_DIR, PID_DIR

    def _wipe() -> None:
        for d in (PID_DIR, LOCK_DIR):
            d.mkdir(parents=True, exist_ok=True)
            for entry in d.iterdir():
                if entry.is_file():
                    try:
                        entry.unlink()
                    except OSError:
                        pass

    _wipe()
    yield
    _wipe()
