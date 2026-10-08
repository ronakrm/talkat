"""Tests for talkat.process_manager — PID lifecycle and lock semantics.

These tests intentionally avoid the cmdline-based is_running() check on
processes other than the current one; that path inspects /proc/<pid>/cmdline
and is heavily coupled to how the OS exposes the test runner's argv.
"""

import os
import subprocess
import time

import pytest

from talkat.process_manager import (
    LockTimeout,
    PIDWriteError,
    ProcessManager,
)

# ---------------------------------------------------------------------------
# Exception class identity
# ---------------------------------------------------------------------------


def test_exception_types_are_distinct_runtime_errors():
    """LockTimeout and PIDWriteError must be distinct RuntimeError subclasses."""
    assert issubclass(LockTimeout, RuntimeError)
    assert issubclass(PIDWriteError, RuntimeError)
    assert LockTimeout is not PIDWriteError


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


def test_is_running_cleans_up_pid_for_non_talkat_process(clean_pid_files):
    """A PID file pointing at a real but non-talkat process must be cleaned up.

    is_running() verifies the PID belongs to a talkat process by scanning
    /proc/<pid>/cmdline. A `sleep` child's cmdline won't contain "talkat",
    so the manager should treat it as stale and remove the file.
    """
    pm = ProcessManager("test")
    sleep_proc = subprocess.Popen(["sleep", "30"])
    try:
        pm.write_pid(sleep_proc.pid)
        running, pid = pm.is_running()
        assert running is False
        assert pid is None
        assert not pm.pid_file.exists()
    finally:
        sleep_proc.terminate()
        sleep_proc.wait(timeout=5)


def test_is_running_cleans_up_pid_file_with_garbage_content(clean_pid_files):
    """Non-numeric PID content must not crash is_running(); file is cleaned up."""
    pm = ProcessManager("test")
    pm.pid_file.parent.mkdir(parents=True, exist_ok=True)
    pm.pid_file.write_text("not a pid")

    running, pid = pm.is_running()
    assert running is False
    assert pid is None
    assert not pm.pid_file.exists()


def test_is_running_cleans_up_empty_pid_file(clean_pid_files):
    """An empty PID file is not a valid running process; clean it up."""
    pm = ProcessManager("test")
    pm.pid_file.parent.mkdir(parents=True, exist_ok=True)
    pm.pid_file.write_text("")

    running, pid = pm.is_running()
    assert running is False
    assert pid is None
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
# write_pid atomic-swap semantics
# ---------------------------------------------------------------------------


def test_write_pid_uses_temp_then_rename(clean_pid_files):
    """write_pid must stage via *.tmp and rename — no temp file should linger."""
    pm = ProcessManager("test_atomic")
    pm.write_pid(12345)
    assert pm.pid_file.exists()
    assert pm.pid_file.read_text() == "12345"
    # The tmp file used during the write must be gone.
    assert not pm.pid_file.with_suffix(".tmp").exists()


def test_write_pid_raises_PIDWriteError_when_dir_unwritable(clean_pid_files):
    """If the PID directory is unwritable, write_pid raises PIDWriteError."""
    if os.geteuid() == 0:
        pytest.skip("Permission-based tests are no-ops when running as root")

    from talkat.paths import PID_DIR

    pm = ProcessManager("test_unwritable")
    original_mode = PID_DIR.stat().st_mode
    try:
        os.chmod(PID_DIR, 0o500)  # r-x only — no write
        with pytest.raises(PIDWriteError):
            pm.write_pid(12345)
        # Neither final nor tmp file should be present.
        assert not pm.pid_file.exists()
        assert not pm.pid_file.with_suffix(".tmp").exists()
    finally:
        os.chmod(PID_DIR, original_mode)


def test_write_pid_cleans_up_tmp_on_rename_failure(clean_pid_files):
    """If rename fails, the staging .tmp file must be removed before raising."""
    pm = ProcessManager("test_rename_fail")
    # Force rename to fail by making the target path a non-empty directory.
    pm.pid_file.mkdir(parents=True, exist_ok=True)
    (pm.pid_file / "marker").write_text("blocking the rename")

    try:
        with pytest.raises(PIDWriteError):
            pm.write_pid(12345)
        # The .tmp file used for staging must have been cleaned up.
        assert not pm.pid_file.with_suffix(".tmp").exists()
    finally:
        # Clean up the directory we created so the fixture wipe works cleanly.
        for child in pm.pid_file.iterdir():
            child.unlink()
        pm.pid_file.rmdir()


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


def test_acquire_lock_timeout_zero_attempts_once(clean_pid_files):
    """Regression: timeout=0 must perform exactly one non-blocking attempt.

    A previous bug would skip the acquire loop entirely when timeout was 0,
    returning False even on an uncontested lock. The fix ensures we always
    try at least once before checking the budget.
    """
    pm = ProcessManager("test")
    assert pm.acquire_lock(timeout=0) is True
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


def test_acquire_lock_failure_closes_fd_so_instance_is_reusable(clean_pid_files):
    """A failed acquire must release the lock fd so the same instance can retry.

    Without this, the second acquire would clobber the still-held fd, leaking
    it and (with the new locked() flow) raising at a confusing place.
    """
    holder = ProcessManager("test")
    holder.acquire_lock(timeout=0.5)
    try:
        contender = ProcessManager("test")
        assert contender.acquire_lock(timeout=0.1) is False
        # Internal fd cleared so a retry on the same instance works once the
        # lock frees up.
        assert contender._lock_fd is None
    finally:
        holder.release_lock()

    # Now the contender can acquire.
    contender = ProcessManager("test")
    assert contender.acquire_lock(timeout=0.5) is True
    contender.release_lock()


def test_context_manager_raises_when_lock_held(clean_pid_files):
    """Regression: __enter__ must raise when the lock cannot be obtained."""
    pm_holder = ProcessManager("test")
    assert pm_holder.acquire_lock(timeout=0.5) is True

    try:
        pm_contender = ProcessManager("test")
        # __enter__ uses the default timeout from config (1.0s) and raises
        # LockTimeout — a RuntimeError subclass.
        with pytest.raises(RuntimeError):
            with pm_contender:
                pass
    finally:
        pm_holder.release_lock()


# ---------------------------------------------------------------------------
# locked() context manager — primary lock primitive from §3
# ---------------------------------------------------------------------------


def test_locked_yields_self_and_releases_on_exit(clean_pid_files):
    """locked() yields the manager and releases the lock on exit."""
    pm = ProcessManager("test")
    with pm.locked() as yielded:
        assert yielded is pm
        assert pm._lock_fd is not None
    # Lock is released after the block.
    assert pm._lock_fd is None


def test_locked_releases_on_exception_in_body(clean_pid_files):
    """If the body raises, locked() still releases the lock."""
    pm = ProcessManager("test")

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with pm.locked():
            raise Boom("oops")

    assert pm._lock_fd is None
    # A subsequent acquisition must succeed.
    with pm.locked():
        pass


def test_locked_raises_LockTimeout_when_held(clean_pid_files):
    """locked() raises LockTimeout (specific class), not a generic RuntimeError."""
    holder = ProcessManager("test")
    holder.acquire_lock(timeout=0.5)
    try:
        contender = ProcessManager("test")
        with pytest.raises(LockTimeout):
            with contender.locked(try_only=True):
                pass
    finally:
        holder.release_lock()


def test_locked_try_only_fails_fast(clean_pid_files):
    """try_only=True must not retry; failure should be near-instant."""
    holder = ProcessManager("test")
    holder.acquire_lock(timeout=0.5)
    try:
        contender = ProcessManager("test")
        start = time.monotonic()
        with pytest.raises(LockTimeout):
            with contender.locked(try_only=True):
                pass
        elapsed = time.monotonic() - start
        # Single non-blocking attempt — should be well under 100ms even on
        # slow CI hardware. Cap at 250ms to avoid flakes.
        assert elapsed < 0.25, f"try_only locked() took {elapsed:.3f}s"
    finally:
        holder.release_lock()


def test_locked_try_only_succeeds_when_uncontested(clean_pid_files):
    """try_only=True must succeed immediately when the lock is free."""
    pm = ProcessManager("test")
    with pm.locked(try_only=True):
        assert pm._lock_fd is not None
    assert pm._lock_fd is None
