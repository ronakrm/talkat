"""Process management for Talkat with proper locking and signal handling."""

import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path

from .logging_config import get_logger

logger = get_logger(__name__)

# Process state directory
RUNTIME_DIR = Path.home() / ".cache" / "talkat"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


class ProcessManager:
    """Manages background processes with proper locking and cleanup."""

    def __init__(self, process_name: str):
        """
        Initialize process manager.

        Args:
            process_name: Name of the process (e.g., 'listen', 'long')
        """
        self.process_name = process_name
        self.pid_file = RUNTIME_DIR / f"{process_name}.pid"
        self.lock_file = RUNTIME_DIR / f"{process_name}.lock"
        self._lock_fd: int | None = None

    def acquire_lock(self, timeout: float = 1.0) -> bool:
        """
        Acquire an exclusive lock for this process.

        Args:
            timeout: Maximum time to wait for lock

        Returns:
            True if lock acquired, False otherwise
        """
        try:
            self._lock_fd = os.open(str(self.lock_file), os.O_CREAT | os.O_WRONLY)

            # Try to acquire lock with timeout
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    logger.debug(f"Acquired lock for {self.process_name}")
                    return True
                except OSError:
                    time.sleep(0.01)

            logger.warning(f"Failed to acquire lock for {self.process_name} within {timeout}s")
            return False

        except Exception as e:
            logger.error(f"Error acquiring lock: {e}")
            return False

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
        Write PID to file with atomic operation.

        Args:
            pid: Process ID to write
        """
        temp_file = self.pid_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w") as f:
                f.write(str(pid))
            temp_file.rename(self.pid_file)
            logger.debug(f"Wrote PID {pid} to {self.pid_file}")
        except Exception as e:
            logger.error(f"Error writing PID file: {e}")
            if temp_file.exists():
                temp_file.unlink()

    def cleanup_pid_file(self) -> None:
        """Remove PID file if it exists."""
        if self.pid_file.exists():
            try:
                self.pid_file.unlink()
                logger.debug(f"Cleaned up PID file {self.pid_file}")
            except Exception as e:
                logger.error(f"Error removing PID file: {e}")

    def stop_process(self, timeout: float = 5.0) -> bool:
        """
        Stop a running process gracefully.

        Args:
            timeout: Maximum time to wait for process to stop

        Returns:
            True if process stopped, False otherwise
        """
        is_running, pid = self.is_running()
        if not is_running or pid is None:
            logger.info(f"No {self.process_name} process to stop")
            return True

        try:
            # First try SIGINT (graceful shutdown)
            logger.info(f"Sending SIGINT to {self.process_name} process (PID: {pid})")
            os.kill(pid, signal.SIGINT)

            # Wait for process to terminate
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    os.kill(pid, 0)  # Check if still running
                    time.sleep(0.1)
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
                time.sleep(0.5)
            except ProcessLookupError:
                pass

            self.cleanup_pid_file()
            return True

        except Exception as e:
            logger.error(f"Error stopping process: {e}")
            return False

    def start_background_process(self, cmd: list[str]) -> int | None:
        """
        Start a background process with proper signal handling.

        Args:
            cmd: Command and arguments to run

        Returns:
            PID of started process or None on failure
        """
        try:
            # Start process in new session to prevent signal propagation
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                # Properly handle signals
                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
            )

            pid = process.pid
            self.write_pid(pid)
            logger.info(f"Started {self.process_name} process with PID {pid}")
            return pid

        except Exception as e:
            logger.error(f"Failed to start process: {e}")
            return None

    def toggle(self) -> tuple[bool, str]:
        """
        Toggle process state (start if stopped, stop if running).

        Returns:
            Tuple of (is_now_running, message)
        """
        if not self.acquire_lock():
            return False, "Another operation is in progress"

        try:
            is_running, pid = self.is_running()

            if is_running:
                # Stop the process
                if self.stop_process():
                    return False, f"Stopped {self.process_name} process"
                else:
                    return True, f"Failed to stop {self.process_name} process"
            else:
                # Start the process (caller needs to provide command)
                return True, f"No {self.process_name} process running, starting new one"

        finally:
            self.release_lock()

    def __enter__(self):
        """Context manager entry."""
        self.acquire_lock()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.release_lock()


def setup_signal_handlers(cleanup_func=None):
    """
    Set up proper signal handlers for graceful shutdown.

    Args:
        cleanup_func: Optional cleanup function to call on shutdown
    """

    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down gracefully")
        if cleanup_func:
            cleanup_func()
        exit(0)

    # Register handlers for common termination signals
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Ignore SIGHUP to prevent termination when terminal closes
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
