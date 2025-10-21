import collections
import contextlib
import os
import warnings
from collections.abc import Generator

import numpy as np
import pyaudio

from .config import load_app_config
from .devices import find_microphone
from .logging_config import get_logger
from .security import safe_subprocess_run

logger = get_logger(__name__)

# Suppress ALSA-specific warnings only
# These are harmless warnings from the ALSA library that we can't control
if os.name == "posix":  # Only on Linux/Unix systems
    from ctypes import CFUNCTYPE, ArgumentError, c_char_p, c_int, cdll

    try:
        # Try to redirect ALSA error messages
        # ALSA error handler signature: void (*handler)(const char *file, int line, const char *function, int err, const char *fmt, ...)
        ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)

        def py_error_handler(filename, line, function, err, fmt):
            pass  # Suppress ALSA errors

        c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
        asound = cdll.LoadLibrary("libasound.so.2")
        asound.snd_lib_error_set_handler(c_error_handler)
    except (OSError, AttributeError, TypeError, ArgumentError):
        # Fallback: suppress PyAudio warnings if ALSA redirect fails
        warnings.filterwarnings("ignore", message=".*ALSA.*", category=RuntimeWarning)
        warnings.filterwarnings("ignore", message=".*jack.*", category=RuntimeWarning)


def calibrate_microphone(duration: int = 10) -> float:
    """Calibrates the microphone to determine an appropriate silence threshold using background noise analysis."""

    # Load audio configuration from config or use defaults
    config = load_app_config()
    CHUNK = config.get("audio_chunk_size", 1024)
    FORMAT = pyaudio.paInt16
    CHANNELS = config.get("audio_channels", 1)
    RATE = config.get("audio_sample_rate", 16000)

    # Show notification if possible
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

    mic_index: int | None = find_microphone()
    if mic_index is None:
        logger.warning("No microphone found during calibration, using default threshold.")
        # Load configuration for fallback threshold
        from .config import CODE_DEFAULTS, load_app_config

        config = load_app_config()
        return float(
            config.get("silence_threshold_fallback", CODE_DEFAULTS["silence_threshold_fallback"])
        )

    p = pyaudio.PyAudio()

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
        # Load configuration for fallback threshold
        from .config import CODE_DEFAULTS, load_app_config

        config = load_app_config()
        return float(
            config.get("silence_threshold_fallback", CODE_DEFAULTS["silence_threshold_fallback"])
        )

    volumes: list[float] = []
    chunks_to_read: int = int(duration * RATE / CHUNK)

    try:
        for i in range(chunks_to_read):
            data = stream.read(CHUNK, exception_on_overflow=False)
            audio_data = np.frombuffer(data, dtype=np.int16)
            # Use float32 to avoid overflow, matching runtime calculation
            volume = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
            volumes.append(volume)

            # Show progress bar
            progress = (i + 1) / chunks_to_read
            bar_length = 40
            filled = int(bar_length * progress)
            bar = "█" * filled + "░" * (bar_length - filled)
            print(
                f"\rProgress: [{bar}] {progress*100:.0f}% | Current: {volume:6.1f}",
                end="",
                flush=True,
            )  # Keep print for real-time progress display
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()
        logger.info("")  # New line after progress bar

    if not volumes:
        # Load configuration for fallback threshold
        from .config import CODE_DEFAULTS, load_app_config

        config = load_app_config()
        return float(
            config.get("silence_threshold_fallback", CODE_DEFAULTS["silence_threshold_fallback"])
        )

    # Calculate statistics using percentiles for better noise floor estimation
    volumes_array = np.array(volumes)

    # Use 90th percentile as the noise floor (ignoring occasional spikes)
    noise_floor: float = float(np.percentile(volumes_array, 90))

    # Also get other percentiles for context
    p50: float = float(np.percentile(volumes_array, 50))  # Median
    p75: float = float(np.percentile(volumes_array, 75))
    p95: float = float(np.percentile(volumes_array, 95))
    p99: float = float(np.percentile(volumes_array, 99))
    max_vol: float = float(np.max(volumes_array))
    min_vol: float = float(np.min(volumes_array))

    # Set threshold as the 95th percentile (ignoring top 5% of noise spikes)
    # This provides a good balance between sensitivity and noise rejection
    threshold: float = p95

    # Load configuration for threshold limits
    from .config import CODE_DEFAULTS, load_app_config

    config = load_app_config()

    threshold_min = config.get("silence_threshold_min", CODE_DEFAULTS["silence_threshold_min"])
    threshold_max = config.get("silence_threshold_max", CODE_DEFAULTS["silence_threshold_max"])

    # Ensure threshold is within configured bounds
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

    # Show notification with result
    with contextlib.suppress(FileNotFoundError):
        safe_subprocess_run(
            ["notify-send", "Calibration Complete", f"Threshold set to {threshold:.0f}"],
            check=False,
            capture_output=True,
        )

    return float(
        max(50.0, min(threshold, 5000.0))
    )  # Clamp between 50.0 and 5000.0 for high-noise environments


def record_audio_with_vad(
    silence_threshold: float | None = None,
    silence_duration: float | None = None,
    debug: bool = True,
) -> tuple[bytes, int] | None:
    """Record with improved VAD: pre-speech padding, defined speech segments, and clear stopping."""
    # Load configuration
    from .config import CODE_DEFAULTS, load_app_config

    config = load_app_config()

    CHUNK = 1024
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 16000

    # VAD Configuration from config
    PRE_SPEECH_PADDING_DURATION: float = config.get(
        "pre_speech_padding", CODE_DEFAULTS["pre_speech_padding"]
    )
    MAX_RECORDING_DURATION_SECONDS: float = config.get(
        "max_recording_duration", CODE_DEFAULTS["max_recording_duration"]
    )

    if silence_threshold is None:
        # This path should ideally not be hit if main.py provides a threshold.
        logger.warning(
            "silence_threshold not provided to record_audio_with_vad. Using a default fallback."
        )
        silence_threshold = config.get(
            "silence_threshold_fallback", CODE_DEFAULTS["silence_threshold_fallback"]
        )

    if silence_duration is None:
        silence_duration = config.get("silence_duration", CODE_DEFAULTS["silence_duration"])

    mic_index: int | None = find_microphone()
    if mic_index is None:
        logger.error("No microphone found!")
        with contextlib.suppress(FileNotFoundError):
            safe_subprocess_run(
                ["notify-send", "Talkat", "No microphone found for recording!"], check=False
            )
        return None

    p: pyaudio.PyAudio = pyaudio.PyAudio()
    stream: pyaudio.Stream | None = None
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
        logger.error(f"Error opening audio stream: {e}")
        if p:
            p.terminate()
        with contextlib.suppress(FileNotFoundError):
            safe_subprocess_run(
                ["notify-send", "Talkat", f"Error opening audio stream: {e}"], check=False
            )
        return None

    logger.info(
        f"Listening with threshold {silence_threshold:.1f}, silence duration {silence_duration:.1f}s..."
    )
    logger.info("Speak now!")

    try:
        safe_subprocess_run(["notify-send", "Talkat", "Listening... Speak now!"], check=False)
    except FileNotFoundError:
        pass  # notify-send is optional

    recorded_audio_segments: list[bytes] = []
    current_segment_frames: list[bytes] = []

    num_pre_padding_chunks: int = int(PRE_SPEECH_PADDING_DURATION * RATE / CHUNK)
    pre_speech_buffer: collections.deque[bytes] = collections.deque(maxlen=num_pre_padding_chunks)

    is_speaking: bool = False
    silent_chunks_count: int = 0
    max_silent_chunks_to_stop: int = int(silence_duration * RATE / CHUNK)

    # Add smoothing for volume detection to avoid false triggers from noise spikes
    SMOOTHING_WINDOW: int = 3  # Number of chunks to average
    volume_history: collections.deque[float] = collections.deque(maxlen=SMOOTHING_WINDOW)

    max_total_chunks: int = int(MAX_RECORDING_DURATION_SECONDS * RATE / CHUNK)
    total_chunks_processed: int = 0

    try:
        while total_chunks_processed < max_total_chunks:
            try:
                data = stream.read(CHUNK, exception_on_overflow=False)
                total_chunks_processed += 1
            except OSError as e:  # More specific exception for stream read errors
                if e.errno == pyaudio.paInputOverflowed:
                    if debug:
                        logger.debug("Input overflowed. Skipping frame.")
                    continue  # Skip this chunk and continue
                logger.error(f"Error reading audio: {e}")
                break  # Critical read error

            audio_data = np.frombuffer(data, dtype=np.int16)
            # Handle empty audio_data if read fails or returns empty
            if audio_data.size == 0:
                if debug:
                    logger.debug("Empty audio data received.")
                continue

            volume = np.sqrt(
                np.mean(audio_data.astype(np.float32) ** 2)
            )  # Use float32 for mean calculation to avoid overflow

            # Add to volume history for smoothing
            volume_history.append(volume)

            # Use smoothed volume (average of recent samples) to reduce noise spikes
            smoothed_volume = float(np.mean(volume_history)) if len(volume_history) > 0 else volume

            if (
                debug and total_chunks_processed % 10 == 0
            ):  # Print more frequently for debugging if needed
                silent_time = silent_chunks_count * CHUNK / RATE
                max_silent_time = max_silent_chunks_to_stop * CHUNK / RATE
                logger.debug(
                    f"Chunk {total_chunks_processed}: Vol: {volume:.1f} Smooth: {smoothed_volume:.1f} (Thr: {silence_threshold:.1f}) Silent: {silent_time:.1f}s/{max_silent_time:.1f}s Speaking: {is_speaking}"
                )

            if smoothed_volume > silence_threshold:
                if not is_speaking:  # Transition to speaking
                    if debug:
                        logger.debug(f"Speech detected! Volume: {volume:.1f}")
                    is_speaking = True
                    current_segment_frames.extend(list(pre_speech_buffer))  # Add pre-buffered audio
                    # pre_speech_buffer.clear() # Clear it after use for this segment

                current_segment_frames.append(data)  # Add current data chunk
                silent_chunks_count = 0  # Reset silence counter
            else:  # volume <= silence_threshold (silence or low noise)
                if is_speaking:
                    # Still considered speaking, but it's a silent part of it.
                    current_segment_frames.append(data)  # Continue recording this silence
                    silent_chunks_count += 1
                    if silent_chunks_count > max_silent_chunks_to_stop:
                        if debug:
                            logger.debug(
                                "Silence duration exceeded after speech, segment finished."
                            )
                        recorded_audio_segments.append(b"".join(current_segment_frames))
                        current_segment_frames = []
                        is_speaking = False  # Reset for potential next utterance, though app breaks
                        break  # Stop after the first full utterance for this app's design
                else:
                    # Still not speaking, keep adding to pre_speech_buffer
                    pre_speech_buffer.append(data)

        if total_chunks_processed >= max_total_chunks:
            if debug:
                logger.debug("Maximum recording duration reached.")

    finally:
        if stream:
            stream.stop_stream()
            stream.close()
        if p:
            p.terminate()

    if debug:
        logger.debug(f"Recording loop finished. Processed {total_chunks_processed} chunks.")

    # If recording was active and current_segment_frames has data (e.g. due to timeout),
    # finalize this last segment.
    if current_segment_frames:
        if debug:
            logger.debug("Finalizing current (potentially incomplete) speech segment.")
        recorded_audio_segments.append(b"".join(current_segment_frames))

    if not recorded_audio_segments:
        if debug:
            logger.debug("No speech segments recorded.")
        with contextlib.suppress(FileNotFoundError):
            safe_subprocess_run(["notify-send", "Talkat", "No speech detected."], check=False)
        return None

    final_audio_data: bytes = b"".join(recorded_audio_segments)

    if not final_audio_data:  # Should be redundant given the check above
        if debug:
            logger.debug("Final audio data is empty.")  # Should not happen
        return None

    if debug:
        logger.debug(f"Recorded {len(final_audio_data)} bytes of audio.")
    return final_audio_data, RATE


# New function for streaming with VAD
def stream_audio_with_vad(
    silence_threshold: float | None = None,
    silence_duration: float = 3.0,  # 3 seconds of silence before stopping
    debug: bool = True,
    chunk_size_ms: int = 30,  # VAD works well with 10, 20, or 30ms frames
    max_duration: float | None = 30.0,  # None for unlimited
) -> Generator[int | bytes, None, None]:
    """Record audio with VAD and yield it in chunks as a generator.
    First yields the sample rate (int), then yields audio data (bytes).
    """
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 16000  # Standard sample rate

    # Calculate chunk samples based on ms, ensure it's an integer for PyAudio
    CHUNK_SAMPLES: int = int(RATE * chunk_size_ms / 1000)

    # Load configuration
    from .config import CODE_DEFAULTS, load_app_config

    config = load_app_config()

    # VAD Configuration from config
    PRE_SPEECH_PADDING_DURATION: float = config.get(
        "pre_speech_padding", CODE_DEFAULTS["pre_speech_padding"]
    )

    # Use configured max duration if not provided
    if max_duration is None:
        max_duration = config.get("max_recording_duration", CODE_DEFAULTS["max_recording_duration"])

    MAX_RECORDING_DURATION_SECONDS: float = (
        max_duration if max_duration is not None else float("inf")
    )

    if silence_threshold is None:
        logger.warning(
            "silence_threshold not provided to stream_audio_with_vad. Using a default fallback."
        )
        silence_threshold = config.get(
            "silence_threshold_fallback", CODE_DEFAULTS["silence_threshold_fallback"]
        )

    # If silence_threshold is 0, disable VAD and stream continuously
    no_vad_mode: bool = silence_threshold == 0

    mic_index: int | None = find_microphone()
    if mic_index is None:
        logger.error("No microphone found for streaming!")
        with contextlib.suppress(FileNotFoundError):
            safe_subprocess_run(
                ["notify-send", "Talkat", "No microphone found for streaming!"], check=False
            )
        return  # End generator if no mic

    p: pyaudio.PyAudio = pyaudio.PyAudio()
    stream: pyaudio.Stream | None = None
    try:
        stream = p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            input_device_index=mic_index,
            frames_per_buffer=CHUNK_SAMPLES,
        )  # Use CHUNK_SAMPLES
    except Exception as e:
        logger.error(f"Error opening audio stream for streaming: {e}")
        if p:
            p.terminate()
        with contextlib.suppress(FileNotFoundError):
            safe_subprocess_run(
                ["notify-send", "Talkat", f"Error opening audio stream: {e}"], check=False
            )
        return  # End generator

    # First, yield the sample rate
    yield RATE

    if no_vad_mode:
        logger.info(
            f"Streaming continuously without VAD (max duration: {MAX_RECORDING_DURATION_SECONDS:.0f}s)..."
        )
    else:
        logger.info(
            f"Streaming with threshold {silence_threshold:.1f}, silence duration {silence_duration:.1f}s..."
        )
    if debug:
        logger.debug("Speak now for streaming!")

    with contextlib.suppress(FileNotFoundError):
        safe_subprocess_run(["notify-send", "Talkat", "Streaming... Speak now!"], check=False)

    num_pre_padding_chunks: int = int(PRE_SPEECH_PADDING_DURATION * RATE / CHUNK_SAMPLES)
    pre_speech_buffer: collections.deque[bytes] = collections.deque(maxlen=num_pre_padding_chunks)

    is_speaking: bool = False
    silent_chunks_count: int = 0
    max_silent_chunks_to_stop: int = int(silence_duration * RATE / CHUNK_SAMPLES)

    # Add smoothing for volume detection to avoid false triggers from noise spikes
    SMOOTHING_WINDOW: int = 3  # Number of chunks to average
    volume_history: collections.deque[float] = collections.deque(maxlen=SMOOTHING_WINDOW)

    max_total_chunks: float | int
    if float("inf") == MAX_RECORDING_DURATION_SECONDS:
        max_total_chunks = float("inf")
    else:
        max_total_chunks = int(MAX_RECORDING_DURATION_SECONDS * RATE / CHUNK_SAMPLES)
    total_chunks_processed: int = 0
    speech_has_started_and_padded: bool = False

    try:
        while total_chunks_processed < max_total_chunks:
            try:
                data = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
                total_chunks_processed += 1
            except OSError as e:
                if e.errno == pyaudio.paInputOverflowed:
                    if debug:
                        logger.debug("Input overflowed during streaming. Skipping frame.")
                    continue
                logger.error(f"Error reading audio for streaming: {e}")
                break

            audio_data_np = np.frombuffer(data, dtype=np.int16)
            if audio_data_np.size == 0:
                if debug:
                    logger.debug("Empty audio data received during streaming.")
                continue

            volume = np.sqrt(np.mean(audio_data_np.astype(np.float32) ** 2))

            # Add to volume history for smoothing
            volume_history.append(volume)

            # Use smoothed volume (average of recent samples) to reduce noise spikes
            smoothed_volume = float(np.mean(volume_history)) if len(volume_history) > 0 else volume

            if (
                debug and total_chunks_processed % (int(1000 / chunk_size_ms) // 2) == 0
            ):  # Log roughly every 0.5s
                silent_time = silent_chunks_count * chunk_size_ms / 1000.0
                max_silent_time = max_silent_chunks_to_stop * chunk_size_ms / 1000.0
                logger.debug(
                    f"Stream chunk {total_chunks_processed}: Vol: {volume:.1f} Smooth: {smoothed_volume:.1f} (Thr: {silence_threshold:.1f}) Silent: {silent_time:.1f}s/{max_silent_time:.1f}s Speaking: {is_speaking}"
                )

            if no_vad_mode:
                # In no-VAD mode, just yield all audio continuously
                yield data
            else:
                # Normal VAD mode - use smoothed volume for decision making
                if smoothed_volume > silence_threshold:
                    if not is_speaking:  # Transition to speaking
                        if debug:
                            logger.debug(f"Speech detected for streaming! Volume: {volume:.1f}")
                        is_speaking = True
                        # Yield pre-buffered audio first
                        yield from list(pre_speech_buffer)
                        pre_speech_buffer.clear()  # Clear after yielding
                        speech_has_started_and_padded = True

                    yield data  # Yield current speech data chunk
                    silent_chunks_count = 0
                else:  # volume <= silence_threshold
                    if is_speaking:
                        # Still considered speaking, but it's a silent part of it.
                        yield data  # Yield this silence as part of the speech
                        silent_chunks_count += 1
                        if silent_chunks_count > max_silent_chunks_to_stop:
                            if debug:
                                logger.debug(
                                    "Silence duration exceeded after speech, stopping stream."
                                )
                            break  # Stop streaming after this utterance
                    elif (
                        not speech_has_started_and_padded
                    ):  # Only buffer if we haven't started speech & padding yet
                        # Still not speaking, keep adding to pre_speech_buffer
                        pre_speech_buffer.append(data)

        if total_chunks_processed >= max_total_chunks:
            if debug:
                logger.debug("Maximum recording duration reached for stream.")

    finally:
        if stream:
            stream.stop_stream()
            stream.close()
        if p:
            p.terminate()
        if debug:
            logger.debug(f"Streaming loop finished. Processed {total_chunks_processed} chunks.")
