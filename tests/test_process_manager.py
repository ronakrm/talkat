"""Tests for talkat.process_manager — PID lifecycle and lock semantics.

These tests intentionally avoid the cmdline-based is_running() check on
processes other than the current one; that path inspects /proc/<pid>/cmdline
and is heavily coupled to how the OS exposes the test runner's argv.
"""

import os

import pytest

from talkat.process_manager import ProcessManager

# ---------------------------------------------------------------------------
# is_running / write_pid / cleanup_pid_file
# ---------------------------------------------------------------------------


def test_is_running_returns_false_when_no_pid_file(clean_pid_files):
    pm = ProcessManager("test")
    assert not pm.pid_file.exists()
    assert pm.is_running() == (False, None)


def test_write_pid_then_is_running_nonexistent_pid_cleans_up(clean_pid_files):
    """A PID file pointing at a dead PID must be cleaned up by is_running()."""
    pm = ProcessManager("test")
    # 99999999 is virtually guaranteed not to exist (max PID on Linux is much lower).
    pm.write_pid(99999999)
    assert pm.pid_file.exists()

    running, pid = pm.is_running()
    assert running is False
    assert pid is None
    # Stale PID file was cleaned up.
    assert not pm.pid_file.exists()


def test_cleanup_pid_file_removes_file(clean_pid_files):
    pm = ProcessManager("test")
    pm.write_pid(os.getpid())
    assert pm.pid_file.exists()

    pm.cleanup_pid_file()
    assert not pm.pid_file.exists()

    # Idempotent: calling again on a missing file is a no-op.
    pm.cleanup_pid_file()
    assert not pm.pid_file.exists()


# ---------------------------------------------------------------------------
# Lock acquire / release
# ---------------------------------------------------------------------------


def test_acquire_and_release_lock(clean_pid_files):
    pm = ProcessManager("test")
    assert pm.acquire_lock(timeout=0.5) is True
    pm.release_lock()

    # After release, we can acquire again on the same instance.
    assert pm.acquire_lock(timeout=0.5) is True
    pm.release_lock()


def test_second_instance_cannot_acquire_held_lock(clean_pid_files):
    """Two ProcessManagers on the same name cannot both hold the lock."""
    pm1 = ProcessManager("test")
    pm2 = ProcessManager("test")

    assert pm1.acquire_lock(timeout=0.5) is True
    try:
        # Short timeout — we expect to fail fast rather than block.
        assert pm2.acquire_lock(timeout=0.2) is False
    finally:
        pm1.release_lock()

    # Once pm1 has released, pm2 can acquire.
    assert pm2.acquire_lock(timeout=0.5) is True
    pm2.release_lock()


def test_context_manager_raises_when_lock_held(clean_pid_files):
    """Regression test for interface fix #9: __enter__ must raise when locked.

    Previously the context manager could silently fail to acquire the lock and
    enter anyway, leading to two callers thinking they each held exclusive
    access. The fix makes __enter__ raise RuntimeError when the lock cannot be
    obtained.
    """
    pm_holder = ProcessManager("test")
    assert pm_holder.acquire_lock(timeout=0.5) is True

    try:
        pm_contender = ProcessManager("test")
        # We override lock_acquire_timeout via env / config in conftest, but the
        # context manager calls acquire_lock() with no arg, so it uses the
        # config default. That default is 1.0s — fine for a test, but to keep
        # this fast we don't worry about it; the contender will block for ~1s
        # and then raise.
        with pytest.raises(RuntimeError):
            with pm_contender:
                pass
    finally:
        pm_holder.release_lock()
