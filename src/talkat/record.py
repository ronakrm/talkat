import collections
import contextlib
import os
import sys
import threading
import time
from collections.abc import Iterator
from types import TracebackType

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


class AudioSession:
    """Context manager that owns the PyAudio + stream lifecycle and yields audio chunks.

    Usage:
        with AudioSession(threshold=200.0) as session:
            rate = session.sample_rate
            for chunk in session:
                ...

    Every chunk is yielded from the moment the stream opens — the threshold
    does NOT gate what is sent (the server-side VAD filter strips leading
    silence far more accurately than an RMS threshold can, and gating is how
    utterance beginnings get clipped). The threshold's only job is deciding
    when the utterance is over: iteration ends when (a) silence persists for
    ``silence_duration`` seconds after speech has been detected, (b)
    ``max_duration`` is reached, or (c) ``stop_event`` is set. Setting
    ``threshold=0`` disables the silence auto-stop entirely.
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
        threshold: float,
        silence_duration: float | None = None,
        max_duration: float | None = None,
        chunk_size_ms: int = 30,
        stop_event: threading.Event | None = None,
        debug: bool = False,
    ):
        config = load_app_config()
        self.threshold = threshold
        self.silence_duration = (
            silence_duration
            if silence_duration is not None
            else config.get("silence_duration", CODE_DEFAULTS["silence_duration"])
        )
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

        no_vad_mode = self.threshold == 0
        max_silent_chunks = int(self.silence_duration * self.SAMPLE_RATE / self._chunk_samples)
        max_total_chunks: float = (
            float("inf")
            if self.max_duration is None
            else int(self.max_duration * self.SAMPLE_RATE / self._chunk_samples)
        )

        smoothing_window: int = 3
        volume_history: collections.deque[float] = collections.deque(maxlen=smoothing_window)

        speech_started = False
        silent_chunks = 0
        total_chunks = 0

        if no_vad_mode:
            logger.info(
                f"Streaming continuously without VAD (max duration: {self.max_duration}s)..."
            )
        else:
            logger.info(
                f"Recording (threshold {self.threshold:.1f}, stops after "
                f"{self.silence_duration:.1f}s of post-speech silence)..."
            )

        while total_chunks < max_total_chunks:
            if self.stop_event is not None and self.stop_event.is_set():
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
                return

            # Every chunk is streamed; the volume tracking below only decides
            # when to stop.
            yield data

            if no_vad_mode:
                continue

            audio_np = np.frombuffer(data, dtype=np.int16)
            if audio_np.size == 0:
                continue

            volume = float(np.sqrt(np.mean(audio_np.astype(np.float32) ** 2)))
            volume_history.append(volume)
            smoothed = float(np.mean(volume_history))

            if self.debug and total_chunks % max(1, int(1000 / self.chunk_size_ms) // 2) == 0:
                silent_time = silent_chunks * self.chunk_size_ms / 1000.0
                max_silent_time = max_silent_chunks * self.chunk_size_ms / 1000.0
                logger.debug(
                    f"Chunk {total_chunks}: Vol: {volume:.1f} Smooth: {smoothed:.1f} "
                    f"(Thr: {self.threshold:.1f}) "
                    f"Silent: {silent_time:.1f}s/{max_silent_time:.1f}s "
                    f"Speech started: {speech_started}"
                )

            if smoothed > self.threshold:
                if not speech_started and self.debug:
                    logger.debug(f"Speech detected. Volume: {volume:.1f}")
                speech_started = True
                silent_chunks = 0
            elif speech_started:
                silent_chunks += 1
                if silent_chunks > max_silent_chunks:
                    if self.debug:
                        logger.debug("Silence duration exceeded, stopping stream.")
                    return

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
