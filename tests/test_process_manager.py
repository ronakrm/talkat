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


# ---------------------------------------------------------------------------
# toggle() — start/stop dispatch under lock
# ---------------------------------------------------------------------------


def test_toggle_calls_start_when_not_running(clean_pid_files):
    """When nothing is running, toggle() invokes start and returns its exit code."""
    pm = ProcessManager("test")
    calls = []

    def start():
        calls.append("start")
        return 0

    rc = pm.toggle(start=start)
    assert rc == 0
    assert calls == ["start"]


def test_toggle_calls_stop_when_running(clean_pid_files, monkeypatch):
    """When a process is running, toggle() must call stop_process and skip start."""
    pm = ProcessManager("test")
    pm.write_pid(os.getpid())

    # Stub is_running so it doesn't depend on /proc/self/cmdline content.
    monkeypatch.setattr(pm, "is_running", lambda: (True, os.getpid()))

    stop_called = []

    def fake_stop(timeout=None):
        stop_called.append(True)
        pm.cleanup_pid_file()
        return True

    monkeypatch.setattr(pm, "stop_process", fake_stop)

    start_called = []

    def start():
        start_called.append(True)
        return 99

    rc = pm.toggle(start=start)
    assert rc == 0
    assert stop_called == [True]
    assert start_called == []


def test_toggle_returns_one_when_stop_fails(clean_pid_files, monkeypatch):
    """Failed stop must surface as exit code 1."""
    pm = ProcessManager("test")
    monkeypatch.setattr(pm, "is_running", lambda: (True, 12345))
    monkeypatch.setattr(pm, "stop_process", lambda timeout=None: False)

    rc = pm.toggle(start=lambda: 0)
    assert rc == 1


def test_toggle_propagates_LockTimeout(clean_pid_files):
    """If the lock is held and try_only=True, toggle() raises LockTimeout."""
    holder = ProcessManager("test")
    holder.acquire_lock(timeout=0.5)
    try:
        contender = ProcessManager("test")
        with pytest.raises(LockTimeout):
            contender.toggle(start=lambda: 0, try_only=True)
    finally:
        holder.release_lock()


def test_rapid_fire_toggle_alternates_start_and_stop(clean_pid_files, monkeypatch):
    """N back-to-back toggle calls must alternate start/stop with no double-start.

    The lock serialises the start/stop decision, so even rapid-fire callers
    must see strictly alternating actions.
    """
    pm = ProcessManager("test")
    state = {"running": False}
    starts = []
    stops = []

    def start():
        starts.append(True)
        state["running"] = True
        return 0

    def fake_is_running():
        return (state["running"], 1 if state["running"] else None)

    def fake_stop(timeout=None):
        stops.append(True)
        state["running"] = False
        return True

    monkeypatch.setattr(pm, "is_running", fake_is_running)
    monkeypatch.setattr(pm, "stop_process", fake_stop)

    for _ in range(6):
        pm.toggle(start=start)

    # Six toggles starting from "not running": start, stop, start, stop, start, stop.
    assert len(starts) == 3
    assert len(stops) == 3
    assert state["running"] is False


# ---------------------------------------------------------------------------
# start_background_process — atomicity with write_pid
# ---------------------------------------------------------------------------


def test_start_background_kills_child_on_pid_write_failure(clean_pid_files, monkeypatch):
    """If write_pid raises after Popen, the spawned child must be killed.

    Otherwise we'd leak an unsupervised child process — exactly the scenario
    §3's atomicity fix is meant to prevent.
    """
    pm = ProcessManager("test")

    def boom(_pid):
        raise PIDWriteError("simulated write failure")

    monkeypatch.setattr(pm, "write_pid", boom)

    # Capture the child Popen so we can assert it died.
    captured: dict = {}
    real_popen = subprocess.Popen

    def wrapped_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(subprocess, "Popen", wrapped_popen)

    # A sleep that would outlive the test if not killed.
    result = pm.start_background_process(["sleep", "30"])

    assert result is None, "start_background_process should return None on PID write failure"
    assert "proc" in captured, "Popen was not invoked"
    proc = captured["proc"]

    # start_background_process should have killed + waited (timeout=2) before
    # returning, so poll() must show termination already.
    assert proc.poll() is not None, f"child {proc.pid} was not killed after write_pid failure"

    # No PID file should have been left behind.
    assert not pm.pid_file.exists()


# ---------------------------------------------------------------------------
# stop_process behaviour when nothing is running
# ---------------------------------------------------------------------------


def test_stop_process_no_op_when_no_pid_file(clean_pid_files):
    """stop_process() on a manager with no PID file returns True and does nothing."""
    pm = ProcessManager("test")
    assert pm.stop_process(timeout=0.1) is True
    assert not pm.pid_file.exists()


def test_stop_process_signals_child_and_cleans_up(clean_pid_files):
    """stop_process() must SIGINT the running child, wait for exit, and remove the PID file.

    We spawn a small Python child whose argv contains the string "talkat" so
    is_running()'s cmdline check accepts it. Python's default SIGINT handler
    raises KeyboardInterrupt, terminating the sleep cleanly.
    """
    pm = ProcessManager("test")

    # The argv suffix ensures /proc/<pid>/cmdline contains "talkat" so
    # is_running() treats this child as one of ours.
    proc = subprocess.Popen(
        [
            "python3",
            "-c",
            "import time; time.sleep(30)",
            "talkat-stop-process-test-marker",
        ]
    )
    try:
        pm.write_pid(proc.pid)

        running, pid = pm.is_running()
        assert running is True
        assert pid == proc.pid

        # SIGINT → KeyboardInterrupt in Python → exit. Allow up to 3s.
        assert pm.stop_process(timeout=3.0) is True

        # Child must actually be dead.
        proc.wait(timeout=2)
        assert proc.poll() is not None

        # PID file cleaned up by stop_process on success.
        assert not pm.pid_file.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
