"""Process management for Talkat with proper locking and signal handling."""

import contextlib
import fcntl
import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import FrameType, TracebackType
from typing import IO, Self

from .logging_config import get_logger
from .paths import LOCK_DIR, LOG_DIR, PID_DIR

logger = get_logger(__name__)


class LockTimeout(RuntimeError):
    """Raised when a process lock cannot be acquired within the timeout."""


class PIDWriteError(RuntimeError):
    """Raised when the atomic PID-file write fails."""


class ProcessManager:
    """Manages background processes with proper locking and cleanup."""

    def __init__(self, process_name: str):
        """
        Initialize process manager.

        Args:
            process_name: Name of the process (e.g., 'listen', 'long')
        """
        self.process_name = process_name
        self.pid_file = PID_DIR / f"{process_name}.pid"
        self.lock_file = LOCK_DIR / f"{process_name}.lock"
        self._lock_fd: int | None = None

    def acquire_lock(self, timeout: float | None = None) -> bool:
        """
        Acquire an exclusive lock for this process.

        Args:
            timeout: Seconds to keep retrying. ``0`` performs a single
                non-blocking attempt. ``None`` uses the configured default.

        Returns:
            True if lock acquired, False otherwise. On failure the underlying
            fd is closed so the manager can be reused.
        """
        if timeout is None:
            from .config import CODE_DEFAULTS, load_app_config

            config = load_app_config()
            timeout = float(
                config.get("lock_acquire_timeout", CODE_DEFAULTS["lock_acquire_timeout"])
            )

        try:
            self._lock_fd = os.open(str(self.lock_file), os.O_CREAT | os.O_WRONLY)
        except OSError as e:
            logger.error(f"Error opening lock file {self.lock_file}: {e}")
            return False

        # Always attempt at least once even if timeout=0; only loop while we
        # still have budget left.
        start_time = time.monotonic()
        retry_interval: float | None = None
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                logger.debug(f"Acquired lock for {self.process_name}")
                return True
            except OSError:
                if time.monotonic() - start_time >= timeout:
                    if timeout > 0:
                        logger.warning(
                            f"Failed to acquire lock for {self.process_name} " f"within {timeout}s"
                        )
                    with contextlib.suppress(OSError):
                        os.close(self._lock_fd)
                    self._lock_fd = None
                    return False
                if retry_interval is None:
                    from .config import CODE_DEFAULTS, load_app_config

                    config = load_app_config()
                    retry_interval = float(
                        config.get("lock_retry_interval", CODE_DEFAULTS["lock_retry_interval"])
                    )
                time.sleep(retry_interval)

    def release_lock(self) -> None:
        """Release the process lock."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                self._lock_fd = None
                logger.debug(f"Released lock for {self.process_name}")
            except Exception as e:
                logger.error(f"Error releasing lock: {e}")

    @contextlib.contextmanager
    def locked(self, try_only: bool = False) -> Iterator["ProcessManager"]:
        """Hold the process lock for the duration of the with block.

        Args:
            try_only: If True, perform a single non-blocking acquire and
                raise immediately if the lock is held by someone else.
                Otherwise use the configured retry timeout.

        Raises:
            LockTimeout: if the lock cannot be acquired.
        """
        timeout = 0.0 if try_only else None
        if not self.acquire_lock(timeout=timeout):
            why = (
                "is held by another talkat command"
                if try_only
                else "could not be acquired within the timeout"
            )
            raise LockTimeout(f"Lock for {self.process_name} {why}")
        try:
            yield self
        finally:
            self.release_lock()

    def is_running(self) -> tuple[bool, int | None]:
        """
        Check if a process is running.

        Returns:
            Tuple of (is_running, pid)
        """
        if not self.pid_file.exists():
            return False, None

        try:
            with open(self.pid_file) as f:
                pid = int(f.read().strip())

            # Check if process exists and is ours
            try:
                # Send signal 0 to check if process exists
                os.kill(pid, 0)

                # Verify it's our process by checking cmdline
                cmdline_path = Path(f"/proc/{pid}/cmdline")
                if cmdline_path.exists():
                    with open(cmdline_path) as f:
                        cmdline = f.read()
                        if "talkat" in cmdline:
                            return True, pid

                # Process exists but isn't ours
                logger.warning(f"PID {pid} exists but is not a talkat process")
                self.cleanup_pid_file()
                return False, None

            except ProcessLookupError:
                # Process doesn't exist, clean up stale PID file
                logger.debug(f"Process {pid} no longer exists")
                self.cleanup_pid_file()
                return False, None

        except (OSError, ValueError) as e:
            logger.error(f"Error reading PID file: {e}")
            self.cleanup_pid_file()
            return False, None

    def write_pid(self, pid: int) -> None:
        """
        Atomically write the PID file.

        Args:
            pid: Process ID to write

        Raises:
            PIDWriteError: on any I/O failure. Callers that spawned a child
                before this call must kill that child when it raises —
                otherwise the child runs unsupervised because nothing tracks
                its PID anymore.
        """
        temp_file = self.pid_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w") as f:
                f.write(str(pid))
            temp_file.rename(self.pid_file)
            logger.debug(f"Wrote PID {pid} to {self.pid_file}")
        except OSError as e:
            logger.error(f"Error writing PID file: {e}")
            with contextlib.suppress(OSError):
                if temp_file.exists():
                    temp_file.unlink()
            raise PIDWriteError(f"Failed to write PID file {self.pid_file}: {e}") from e

    def cleanup_pid_file(self) -> None:
        """Remove PID file if it exists."""
        if self.pid_file.exists():
            try:
                self.pid_file.unlink()
                logger.debug(f"Cleaned up PID file {self.pid_file}")
            except Exception as e:
                logger.error(f"Error removing PID file: {e}")

    def stop_process(self, timeout: float | None = None) -> bool:
        """
        Stop a running process gracefully.

        Args:
            timeout: Maximum time to wait for process to stop (uses config default if None)

        Returns:
            True if process stopped, False otherwise
        """
        if timeout is None:
            from .config import CODE_DEFAULTS, load_app_config

            config = load_app_config()
            timeout = config.get("process_stop_timeout", CODE_DEFAULTS["process_stop_timeout"])

        is_running, pid = self.is_running()
        if not is_running or pid is None:
            logger.info(f"No {self.process_name} process to stop")
            return True

        try:
            # First try SIGINT (graceful shutdown)
            logger.info(f"Sending SIGINT to {self.process_name} process (PID: {pid})")
            os.kill(pid, signal.SIGINT)

            # Load check interval from config
            from .config import CODE_DEFAULTS, load_app_config

            config = load_app_config()
            check_interval = config.get(
                "process_check_interval", CODE_DEFAULTS["process_check_interval"]
            )

            # Wait for process to terminate
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    os.kill(pid, 0)  # Check if still running
                    time.sleep(check_interval)
                except ProcessLookupError:
                    # Process terminated
                    logger.info(f"Process {pid} stopped gracefully")
                    self.cleanup_pid_file()
                    return True

            # If still running, try SIGTERM
            logger.warning(f"Process {pid} didn't stop gracefully, sending SIGTERM")
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)

            # Final check
            try:
                os.kill(pid, 0)
                # Still running, force kill
                logger.error(f"Process {pid} won't stop, sending SIGKILL")
                os.kill(pid, signal.SIGKILL)
                background_delay = config.get(
                    "background_process_delay", CODE_DEFAULTS["background_process_delay"]
                )
                time.sleep(background_delay)
            except ProcessLookupError:
                pass

            self.cleanup_pid_file()
            return True

        except Exception as e:
            logger.error(f"Error stopping process: {e}")
            return False

    def start_background_process(
        self, cmd: list[str], debug: bool = False, env: dict | None = None
    ) -> int | None:
        """
        Start a background process with proper signal handling.

        Atomicity: the child is spawned, then its PID is written to the PID
        file. If the write fails the child is killed so we never leak an
        unsupervised process. The window between Popen returning and write_pid
        is small (microseconds) but is also covered by the child's own
        self-registration in listen_continuous as a defence-in-depth measure.

        Args:
            cmd: Command and arguments to run
            debug: If True, redirect output to log files instead of DEVNULL
            env: Optional environment variables to pass to the process

        Returns:
            PID of started process, or None on failure (child is killed if the
            failure happens after Popen).
        """
        log_handle: IO[str] | None = None
        try:
            if debug:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                log_file = LOG_DIR / f"{self.process_name}_debug.log"
                logger.info(f"Debug mode: output will be written to {log_file}")
                log_handle = open(log_file, "a")
                stdout: int | IO[str] = log_handle
                stderr: int | IO[str] = log_handle
            else:
                stdout = subprocess.DEVNULL
                stderr = subprocess.DEVNULL

            process = subprocess.Popen(
                cmd,
                stdout=stdout,
                stderr=stderr,
                env=env,
                start_new_session=True,
                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
            )

            pid = process.pid
            try:
                self.write_pid(pid)
            except PIDWriteError:
                # The child is running but we couldn't record its PID — that
                # would leave an orphan. Kill it so the user can retry from a
                # clean state.
                logger.error(f"Could not record PID for child {pid}; terminating it")
                with contextlib.suppress(Exception):
                    process.kill()
                    process.wait(timeout=2)
                return None

            logger.info(f"Started {self.process_name} process with PID {pid}")
            return pid

        except Exception as e:
            logger.error(f"Failed to start process: {e}")
            return None
        finally:
            # Popen dup'd the fd into the child; the parent's handle is no
            # longer needed and would otherwise leak.
            if log_handle is not None:
                log_handle.close()

    def toggle(
        self,
        start: Callable[[], int],
        try_only: bool = False,
    ) -> int:
        """
        Toggle the managed process under lock.

        If running, stop it. If not running, invoke ``start`` (also under the
        lock) and return its exit code.

        ``start`` MUST be non-blocking — it runs while the lock is held, so
        long-running foreground work must NOT use this method. Use
        :meth:`locked` directly and release the lock before doing the long
        work.

        Args:
            start: Callback invoked when no process is running. Must return an
                exit code and must not block on long-running work.
            try_only: If True, fail immediately if the lock is held instead of
                waiting.

        Returns:
            Exit code (0 for success). On stop failure returns 1. Raises
            LockTimeout if the lock cannot be acquired.
        """
        with self.locked(try_only=try_only):
            is_running, _ = self.is_running()
            if is_running:
                return 0 if self.stop_process() else 1
            return start()

    def __enter__(self) -> Self:
        """Convenience equivalent to ``with self.locked():`` (no try_only)."""
        if not self.acquire_lock():
            raise LockTimeout(
                f"Could not acquire lock for {self.process_name} "
                f"(another talkat command may be in progress)"
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Context manager exit."""
        self.release_lock()


def setup_signal_handlers(cleanup_func: Callable[[], None] | None = None) -> None:
    """
    Set up proper signal handlers for graceful shutdown.

    Args:
        cleanup_func: Optional cleanup function to call on shutdown
    """

    def signal_handler(signum: int, frame: FrameType | None) -> None:
        logger.info(f"Received signal {signum}, shutting down gracefully")
        if cleanup_func:
            cleanup_func()
        exit(0)

    # Register handlers for common termination signals
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Ignore SIGHUP to prevent termination when terminal closes
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
