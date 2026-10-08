import contextlib
import os
import sys
import threading
import time
from collections.abc import Iterator
from types import TracebackType
from typing import Literal

import numpy as np
import pyaudio

from .config import CODE_DEFAULTS, load_app_config
from .devices import find_microphone
from .logging_config import get_logger
from .security import safe_subprocess_run

logger = get_logger(__name__)


@contextlib.contextmanager
def _suppress_native_stderr() -> Iterator[None]:
    """Redirect stderr at the fd level to /dev/null for the duration of the block.

    Suppresses noisy messages from ALSA / JACK / PortAudio that go straight to
    file descriptor 2 from C and bypass Python's logging. This replaces the
    older ctypes-libasound hack: it works on non-glibc systems (musl), doesn't
    depend on a specific libasound symbol, and silences JACK + PortAudio noise
    too. Errors that matter still raise Python exceptions, which are handled
    by the caller's try/except.
    """
    # If the interpreter is running without stderr (e.g. early in a daemonized
    # process) we have nothing to redirect.
    try:
        stderr_fd = sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        yield
        return

    saved_fd = os.dup(stderr_fd)
    try:
        with open(os.devnull, "wb") as devnull:
            os.dup2(devnull.fileno(), stderr_fd)
            try:
                yield
            finally:
                os.dup2(saved_fd, stderr_fd)
    finally:
        os.close(saved_fd)


class AudioSessionError(RuntimeError):
    """Raised when the microphone or audio stream can't be opened."""


# Why AudioSession iteration ended. Only "stop_requested" means the user
# asked; the others end a recording on their own.
StopReason = Literal["stop_requested", "max_duration", "read_error"]


class AudioSession:
    """Context manager that owns the PyAudio + stream lifecycle and yields audio chunks.

    Usage:
        with AudioSession(max_duration=600.0) as session:
            rate = session.sample_rate
            for chunk in session:
                ...

    Every chunk is yielded from the moment the stream opens; nothing here is
    gated on level. Deciding what is speech belongs to the segmenter (where
    to cut) and the server-side VAD filter (what to trim) — dropping "quiet"
    audio at capture is exactly how utterance beginnings used to get clipped.

    Iteration ends when ``stop_event`` is set, ``max_duration`` is reached, or
    the stream fails; ``stop_reason`` says which.
    """

    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    SAMPLE_RATE = 16000

    # A stream-open can race PipeWire device hotplug; one re-enumeration
    # retry absorbs nearly all transient failures without meaningful delay.
    OPEN_ATTEMPTS = 2
    OPEN_RETRY_DELAY_S = 0.2

    def __init__(
        self,
        max_duration: float | None = None,
        chunk_size_ms: int = 30,
        stop_event: threading.Event | None = None,
        debug: bool = False,
    ):
        config = load_app_config()
        self.max_duration = (
            max_duration
            if max_duration is not None
            else config.get("max_recording_duration", CODE_DEFAULTS["max_recording_duration"])
        )
        self.input_device_name: str | None = config.get("input_device_name") or None
        self.chunk_size_ms = chunk_size_ms
        self.stop_event = stop_event
        self.debug = debug

        self.sample_rate = self.SAMPLE_RATE
        self.stop_reason: StopReason | None = None
        self._chunk_samples = int(self.SAMPLE_RATE * chunk_size_ms / 1000)
        self._p: pyaudio.PyAudio | None = None
        self._stream: pyaudio.Stream | None = None

    def __enter__(self) -> "AudioSession":
        last_error: Exception | None = None
        for attempt in range(self.OPEN_ATTEMPTS):
            if attempt > 0:
                time.sleep(self.OPEN_RETRY_DELAY_S)
            with _suppress_native_stderr():
                # A fresh PyAudio instance per attempt: PortAudio snapshots
                # the device topology at instantiation, so the device index
                # is resolved and opened against the same snapshot.
                p = pyaudio.PyAudio()
                mic_index = find_microphone(p, preferred_name=self.input_device_name)
                if mic_index is None:
                    p.terminate()
                    raise AudioSessionError("No microphone found")
                try:
                    self._stream = p.open(
                        format=self.FORMAT,
                        channels=self.CHANNELS,
                        rate=self.SAMPLE_RATE,
                        input=True,
                        input_device_index=mic_index,
                        frames_per_buffer=self._chunk_samples,
                    )
                    self._p = p
                    return self
                except Exception as e:
                    p.terminate()
                    last_error = e
            logger.warning(
                f"Audio stream open failed (attempt {attempt + 1}/{self.OPEN_ATTEMPTS}): "
                f"{last_error}"
            )
        raise AudioSessionError(f"Failed to open audio stream: {last_error}") from last_error

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.stop_stream()
            with contextlib.suppress(Exception):
                self._stream.close()
            self._stream = None
        if self._p is not None:
            with contextlib.suppress(Exception):
                self._p.terminate()
            self._p = None

    def __iter__(self) -> Iterator[bytes]:
        if self._stream is None:
            raise RuntimeError("AudioSession must be used as a context manager")

        max_total_chunks: float = (
            float("inf")
            if self.max_duration is None
            else int(self.max_duration * self.SAMPLE_RATE / self._chunk_samples)
        )
        total_chunks = 0
        logger.info(f"Recording (up to {self.max_duration:g}s)...")

        while total_chunks < max_total_chunks:
            if self.stop_event is not None and self.stop_event.is_set():
                logger.info("Stop requested — finishing this recording...")
                self.stop_reason = "stop_requested"
                return

            try:
                data = self._stream.read(self._chunk_samples, exception_on_overflow=False)
                total_chunks += 1
            except OSError as e:
                if e.errno == pyaudio.paInputOverflowed:
                    if self.debug:
                        logger.debug("Input overflowed. Skipping frame.")
                    continue
                logger.error(f"Error reading audio: {e}")
                self.stop_reason = "read_error"
                return

            yield data

        logger.info(f"Recording stopped: reached the {self.max_duration:g}s length limit.")
        self.stop_reason = "max_duration"
        if self.debug:
            logger.debug(f"Streaming loop finished. Processed {total_chunks} chunks.")


def calibrate_microphone(duration: int = 10) -> float:
    """Calibrates the microphone to determine an appropriate silence threshold using background noise analysis."""

    config = load_app_config()
    CHUNK = 1024
    FORMAT = AudioSession.FORMAT
    CHANNELS = AudioSession.CHANNELS
    RATE = AudioSession.SAMPLE_RATE

    with contextlib.suppress(FileNotFoundError):
        safe_subprocess_run(
            [
                "notify-send",
                "Talkat Calibration",
                f"Measuring background noise for {duration} seconds. Please remain quiet.",
            ],
            check=False,
            capture_output=True,
        )

    logger.info("\n" + "=" * 60)
    logger.info("MICROPHONE CALIBRATION - Background Noise Analysis")
    logger.info("=" * 60)
    logger.info(f"Please remain QUIET during calibration ({duration} seconds).")
    logger.info("Measuring ambient noise levels...")
    logger.info("-" * 60)

    with _suppress_native_stderr():
        p = pyaudio.PyAudio()
        # Same-instance resolve + open — see find_microphone for the race
        # this avoids.
        mic_index: int | None = find_microphone(
            p, preferred_name=config.get("input_device_name") or None
        )
        if mic_index is None:
            logger.warning("No microphone found during calibration, using default threshold.")
            p.terminate()
            return float(
                config.get(
                    "silence_threshold_fallback", CODE_DEFAULTS["silence_threshold_fallback"]
                )
            )

        try:
            stream = p.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                input_device_index=mic_index,
                frames_per_buffer=CHUNK,
            )
        except Exception as e:
            logger.error(f"Error opening audio stream for calibration: {e}")
            p.terminate()
            return float(
                config.get(
                    "silence_threshold_fallback", CODE_DEFAULTS["silence_threshold_fallback"]
                )
            )

    volumes: list[float] = []
    chunks_to_read: int = int(duration * RATE / CHUNK)

    try:
        for i in range(chunks_to_read):
            data = stream.read(CHUNK, exception_on_overflow=False)
            audio_data = np.frombuffer(data, dtype=np.int16)
            volume = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
            volumes.append(volume)

            progress = (i + 1) / chunks_to_read
            bar_length = 40
            filled = int(bar_length * progress)
            bar = "█" * filled + "░" * (bar_length - filled)
            print(
                f"\rProgress: [{bar}] {progress * 100:.0f}% | Current: {volume:6.1f}",
                end="",
                flush=True,
            )
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()
        logger.info("")

    if not volumes:
        return float(
            config.get("silence_threshold_fallback", CODE_DEFAULTS["silence_threshold_fallback"])
        )

    volumes_array = np.array(volumes)

    noise_floor: float = float(np.percentile(volumes_array, 90))
    p50: float = float(np.percentile(volumes_array, 50))
    p75: float = float(np.percentile(volumes_array, 75))
    p95: float = float(np.percentile(volumes_array, 95))
    p99: float = float(np.percentile(volumes_array, 99))
    max_vol: float = float(np.max(volumes_array))
    min_vol: float = float(np.min(volumes_array))

    threshold: float = p95

    threshold_min = config.get("silence_threshold_min", CODE_DEFAULTS["silence_threshold_min"])
    threshold_max = config.get("silence_threshold_max", CODE_DEFAULTS["silence_threshold_max"])

    threshold = max(threshold, threshold_min)
    threshold = min(threshold, threshold_max)

    logger.info("\n" + "-" * 60)
    logger.info("CALIBRATION RESULTS:")
    logger.info("-" * 60)
    logger.info("  Background Noise Analysis:")
    logger.info(f"    Min volume:         {min_vol:8.1f}")
    logger.info(f"    50th percentile:    {p50:8.1f} (median)")
    logger.info(f"    75th percentile:    {p75:8.1f}")
    logger.info(f"    90th percentile:    {noise_floor:8.1f} ← NOISE FLOOR")
    logger.info(f"    95th percentile:    {p95:8.1f}")
    logger.info(f"    99th percentile:    {p99:8.1f}")
    logger.info(f"    Max volume:         {max_vol:8.1f}")
    logger.info(f"\n  Recommended threshold: {threshold:8.1f}")
    logger.info("  (95th percentile - ignores top 5% noise spikes)")
    logger.info("=" * 60)

    with contextlib.suppress(FileNotFoundError):
        safe_subprocess_run(
            ["notify-send", "Calibration Complete", f"Threshold set to {threshold:.0f}"],
            check=False,
            capture_output=True,
        )

    return float(max(50.0, min(threshold, 5000.0)))
