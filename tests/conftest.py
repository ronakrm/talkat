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

# ---------------------------------------------------------------------------
# Live AIPP test opt-in (§5a follow-up)
# ---------------------------------------------------------------------------
#
# Tests decorated with @pytest.mark.aipp_live are skipped by default. They
# hit a real OpenAI-compatible backend (Ollama, llama.cpp server, etc.) and
# need a server already running and a model already pulled. Run them with:
#
#     uv run pytest --aipp-live
#
# Configure via env vars (defaults match a fresh Ollama install):
#     OLLAMA_BASE_URL  http://localhost:11434/v1
#     OLLAMA_MODEL     qwen2.5:0.5b


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--aipp-live",
        action="store_true",
        default=False,
        help="Run AIPP tests that require a live Ollama-compatible backend.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "aipp_live: requires a live OpenAI-compatible backend (e.g. Ollama). "
        "Off by default; pass --aipp-live to enable.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--aipp-live"):
        return
    skip_live = pytest.mark.skip(reason="needs --aipp-live (set up a local backend first)")
    for item in items:
        if "aipp_live" in item.keywords:
            item.add_marker(skip_live)


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
