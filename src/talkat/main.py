#!/usr/bin/env python3

import contextlib
import json
import os
import signal
import threading
import time
import traceback
from collections.abc import Callable, Generator
from datetime import datetime
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any

import httpx

from .clipboard import copy_to_clipboard
from .config import CODE_DEFAULTS, load_app_config, save_app_config
from .diagnostics import build_record, write_record
from .focus import get_focused_window
from .logging_config import get_logger
from .paths import TRANSCRIPT_DIR
from .process_manager import ProcessManager
from .record import AudioSession, AudioSessionError, calibrate_microphone
from .security import safe_subprocess_run, sanitize_text_for_typing

logger = get_logger(__name__)


class TranscriptionUnreachable(RuntimeError):
    """Server unreachable — connection refused, DNS failure, or request timeout."""


class TranscriptionServerError(RuntimeError):
    """Server returned an error (non-2xx, malformed JSON, etc.)."""


def get_transcript_dir() -> Path:
    """Get or create the transcript directory."""
    config = load_app_config()
    transcript_dir_str = config.get("transcript_dir", str(TRANSCRIPT_DIR))
    transcript_dir = Path(os.path.expanduser(transcript_dir_str))
    transcript_dir.mkdir(parents=True, exist_ok=True)
    return transcript_dir


def save_transcript(text: str, mode: str = "short") -> Path:
    """Save transcript to a file with timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{mode}.txt"
    filepath = get_transcript_dir() / filename

    with open(filepath, "a", encoding="utf-8") as f:
        f.write(text + "\n")

    return filepath


def _notify(message: str) -> None:
    """Send a desktop notification; a notification failure must never break dictation."""
    try:
        safe_subprocess_run(["notify-send", "Talkat", message], check=False)
    except Exception as e:
        logger.debug(f"Notification failed: {e}")


def _log_threshold_source(threshold: float) -> None:
    """Emit a one-line info log about where the threshold came from."""
    if threshold == CODE_DEFAULTS["silence_threshold"]:
        logger.info(f"No calibrated threshold found in config. Using default: {threshold:.1f}")
        logger.info("Run 'talkat calibrate' to set a custom threshold.")
    else:
        logger.info(f"Using threshold: {threshold:.1f} (from config)")


class TranscriptionClient:
    """Records a single utterance from the microphone and POSTs it to the model server."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.socket_path: str = config.get("server_socket", CODE_DEFAULTS["server_socket"])
        self.http_timeout: int = int(config.get("http_timeout", CODE_DEFAULTS["http_timeout"]))
        self.threshold: float = float(
            config.get("silence_threshold", CODE_DEFAULTS["silence_threshold"])
        )
        self.silence_duration: float = float(
            config.get("silence_duration", CODE_DEFAULTS["silence_duration"])
        )
        # Per-request language override sent in the stream metadata. The
        # server has its own config default; we only send this if the client
        # has one configured, so an older server build still works.
        self.language: str | None = config.get("language")
        # Server response metadata from the most recent transcribe call —
        # audio duration, applied gain, ASR wall-clock. Populated after every
        # ``transcribe_one_utterance``; old servers without these fields just
        # leave the values at 0 / 0.0.
        self.last_metadata: dict[str, float] = {
            "audio_duration": 0.0,
            "applied_gain_db": 0.0,
            "asr_seconds": 0.0,
        }
        transport = httpx.HTTPTransport(uds=self.socket_path)
        self._client = httpx.Client(transport=transport, timeout=self.http_timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TranscriptionClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def transcribe_one_utterance(
        self,
        stop_event: threading.Event | None = None,
        max_duration: float | None = None,
        debug: bool = False,
        on_recording_started: Callable[[], None] | None = None,
    ) -> str:
        """
        Record one utterance and return its transcription.

        ``on_recording_started`` fires once the microphone stream is actually
        open — the earliest moment speech is being captured. "Start speaking"
        cues belong there; firing them earlier (before device setup, which
        takes hundreds of ms) is how utterance beginnings get lost.

        Returns the transcribed text (possibly empty if no speech detected).
        Raises ``AudioSessionError`` on microphone failure, ``TranscriptionUnreachable``
        if the server is unreachable, and ``TranscriptionServerError`` for other
        server-side problems.
        """
        with AudioSession(
            threshold=self.threshold,
            silence_duration=self.silence_duration,
            max_duration=max_duration,
            stop_event=stop_event,
            debug=debug,
        ) as session:
            if on_recording_started is not None:
                on_recording_started()
            metadata: dict[str, Any] = {"rate": session.sample_rate}
            if self.language:
                metadata["language"] = self.language

            def body() -> Generator[bytes, None, None]:
                yield json.dumps(metadata).encode("utf-8") + b"\n"
                for chunk in session:
                    if chunk:
                        yield chunk

            # The host part of the URL is ignored when using a unix-socket transport;
            # only the path matters. We use a placeholder host purely for httpx hygiene.
            try:
                response = self._client.post("http://talkat/transcribe_stream", content=body())
                response.raise_for_status()
            except httpx.ConnectError as e:
                raise TranscriptionUnreachable(
                    f"Could not connect to the model server at {self.socket_path}. "
                    "Ensure it's running: systemctl --user status talkat"
                ) from e
            except httpx.TimeoutException as e:
                raise TranscriptionUnreachable(f"Request to model server timed out: {e}") from e
            except httpx.HTTPError as e:
                raise TranscriptionServerError(f"Error communicating with model server: {e}") from e

            try:
                payload = response.json()
            except json.JSONDecodeError as e:
                raise TranscriptionServerError(
                    f"Could not decode JSON response from server: {response.text}"
                ) from e

            # Cache server-side metadata for diagnostics. Missing keys (older
            # server build that pre-dates this) collapse to 0 — fine; the
            # diagnostics record just shows zeros instead of crashing.
            self.last_metadata = {
                "audio_duration": float(payload.get("audio_duration", 0.0) or 0.0),
                "applied_gain_db": float(payload.get("applied_gain_db", 0.0) or 0.0),
                "asr_seconds": float(payload.get("asr_seconds", 0.0) or 0.0),
            }
            return str(payload.get("text", "")).strip()


def _fetch_server_info(socket_path: str) -> tuple[str | None, str | None]:
    """Best-effort fetch of model_type / model_name from /health for diagnostics."""
    try:
        transport = httpx.HTTPTransport(uds=socket_path)
        with httpx.Client(transport=transport, timeout=2.0) as client:
            r = client.get("http://talkat/health")
            if r.status_code != 200:
                return None, None
            data = r.json()
            return data.get("model_type"), data.get("model_name")
    except (httpx.HTTPError, json.JSONDecodeError, OSError):
        return None, None


def _set_stop_event_on_signal(stop_event: threading.Event) -> None:
    """
    Install signal handlers that set an Event and interrupt blocking I/O.

    The handler does NO logging or cleanup — Python's logging module isn't
    async-signal-safe and can deadlock if invoked from a handler. The main
    loop observes the event and runs logging/cleanup itself.

    The handler also **raises ``KeyboardInterrupt``** at the end. Without
    this, PEP 475's auto-retry of EINTR'd syscalls means a SIGINT arriving
    during a blocking ``httpx.post`` (transcribe call) is silently swallowed
    and we wait for the full ``http_timeout`` (default 120 s) before noticing
    the user asked us to stop. Raising from the handler propagates out via
    Python's standard exception path — async-signal-safe in the same way the
    interpreter's built-in SIGINT handler is. Callers catch and exit cleanly.
    """

    def handler(signum: int, frame: FrameType | None) -> None:
        stop_event.set()
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, handler)
    with contextlib.suppress(ValueError):
        # SIGTERM/SIGHUP may not be settable on all platforms / from non-main threads.
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)


def run_calibrate() -> int:
    """Run microphone calibration and persist the resulting threshold."""
    logger.info("Starting microphone calibration...")
    threshold = calibrate_microphone()

    config = load_app_config()
    config["silence_threshold"] = threshold
    save_app_config(config)

    logger.info(f"Calibration complete. Threshold set to: {threshold:.1f}")
    _notify(f"Calibration complete. Threshold: {threshold:.1f}")
    return 0


def listen_once(
    output_file: str | None = None,
    config_overrides: dict[str, Any] | None = None,
    postprocess: str | None = None,
) -> int:
    """Record one utterance and either type it (default) or save it to a file.

    The caller (cli.py) is responsible for acquiring the listen process lock,
    deciding to start vs. stop, and writing this process's PID into the PID
    file under that lock. We only clean up on exit.

    When ``postprocess`` is set, the transcript is piped through the named
    AIPP profile before output. AIPP is fail-open — a misconfigured profile
    or unreachable LLM falls back to typing the raw transcript with a
    notification, so the dictation is never lost.
    """
    config = load_app_config()
    if config_overrides:
        config.update(config_overrides)

    pm = ProcessManager("listen")

    stop_event = threading.Event()
    _set_stop_event_on_signal(stop_event)

    _log_threshold_source(
        float(config.get("silence_threshold", CODE_DEFAULTS["silence_threshold"]))
    )

    # The focus guard compares against the window focused at invocation time —
    # that's the window the user intends to dictate into.
    focus_before: str | None = None
    if config.get("focus_guard", CODE_DEFAULTS["focus_guard"]):
        focus_before = get_focused_window()

    def _announce_recording() -> None:
        logger.info("Recording — speak now. (Run 'talkat listen' again to stop.)")
        _notify('Recording... Run "talkat listen" again to stop')

    server_metadata: dict[str, float] = {
        "audio_duration": 0.0,
        "applied_gain_db": 0.0,
        "asr_seconds": 0.0,
    }
    try:
        with TranscriptionClient(config) as client:
            text = client.transcribe_one_utterance(
                stop_event=stop_event,
                debug=True,
                on_recording_started=_announce_recording,
            )
            # ``last_metadata`` is set by the real client after each call;
            # test stubs may not have it. Default to zeros so diagnostics
            # still write a valid record.
            server_metadata = getattr(client, "last_metadata", server_metadata)
    except AudioSessionError as e:
        logger.error(str(e))
        _notify(f"Audio error: {e}")
        pm.cleanup_pid_file()
        return 1
    except TranscriptionUnreachable as e:
        logger.error(str(e))
        _notify("Error: Model server not reachable.")
        pm.cleanup_pid_file()
        return 1
    except TranscriptionServerError as e:
        logger.error(str(e))
        _notify(f"Server communication error: {e}")
        pm.cleanup_pid_file()
        return 1
    except KeyboardInterrupt:
        logger.info("Recording interrupted.")
        pm.cleanup_pid_file()
        return 0

    if not text:
        logger.warning("No text recognized in the audio")
        _notify("No text recognized")
        pm.cleanup_pid_file()
        return 0

    logger.info(f"Recognized: {text}")

    if config.get("save_transcripts", True):
        transcript_path = save_transcript(text, mode="short")
        logger.info(f"Transcript saved to: {transcript_path}")

    if postprocess:
        from .postprocess import postprocess_text

        text = postprocess_text(text, postprocess, config=config)
        # postprocess_text fails open — text is the AIPP output on success,
        # the original transcript on failure. Either way it's typable.

    if output_file:
        output_path = Path(output_file).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        logger.info(f"Transcription saved to: {output_path}")
        _notify(f"Saved to: {output_path.name}")
    else:
        _deliver_text(text, config, focus_before)

    _write_diagnostics_for_listen(
        config=config,
        mode="listen",
        text=text,
        server_metadata=server_metadata,
        postprocess=postprocess,
    )

    pm.cleanup_pid_file()
    return 0


def _deliver_text(text: str, config: dict[str, Any], focus_before: str | None) -> None:
    """Deliver the transcript: type it into the focused window, else clipboard.

    The invariant this function maintains: the transcript is never silently
    lost. Every failure path lands it in the clipboard or, failing that, on
    stdout — with a notification saying where it went.
    """
    if config.get("output_mode", CODE_DEFAULTS["output_mode"]) == "clipboard":
        if copy_to_clipboard(text):
            logger.info(f"Copied to clipboard: {text}")
            _notify(f"Copied: {text[:100]}")
        else:
            logger.warning("Clipboard unavailable (wl-copy/xclip missing), printing instead:")
            print(f"TEXT: {text}")
            _notify(f"Recognized: {text[:100]}")
        return

    # Focus guard: if the user switched windows while dictating, typing would
    # splatter the transcript into the wrong app. Both sides must be known to
    # conclude "changed" — an IPC failure disables the guard, not typing.
    if focus_before is not None:
        focus_now = get_focused_window()
        if focus_now is not None and focus_now != focus_before:
            logger.warning(
                f"Focused window changed during dictation ({focus_before} -> {focus_now}); "
                "not typing into it."
            )
            if copy_to_clipboard(text):
                _notify("Focus changed — transcript copied to clipboard.")
            else:
                print(f"TEXT: {text}")
                _notify("Focus changed — transcript printed to console.")
            return

    try:
        safe_text = sanitize_text_for_typing(text)
        # timeout=None: typing a long transcript with --key-delay=1 can take
        # well over the default 30s.
        safe_subprocess_run(
            ["ydotool", "type", "--key-delay=1", safe_text],
            check=True,
            timeout=None,
        )
        logger.info(f"Typed: {text}")
        _notify(f"Typed: {text[:100]}")
    except Exception as e:
        # Typing fails in many ways — ydotool missing, ydotoold not running,
        # a crash mid-type. The transcript must survive all of them.
        logger.warning(f"Typing failed ({e}); falling back to clipboard.")
        if copy_to_clipboard(text):
            _notify("Typing failed — transcript copied to clipboard.")
        else:
            print(f"TEXT: {text}")
            _notify(f"Recognized: {text[:100]}")


def _write_diagnostics_for_listen(
    *,
    config: dict[str, Any],
    mode: str,
    text: str,
    server_metadata: dict[str, float],
    postprocess: str | None,
) -> None:
    """Build + write a diagnostics record for an interactive dictation run.

    Diagnostics are advisory: any failure inside is swallowed so the user
    never loses dictation because a JSON write hit ENOSPC or similar.
    """
    try:
        socket_path = config.get("server_socket", CODE_DEFAULTS["server_socket"])
        model_type, model_name = _fetch_server_info(socket_path)
        record = build_record(
            mode=mode,
            audio_duration=server_metadata.get("audio_duration", 0.0),
            asr_seconds=server_metadata.get("asr_seconds", 0.0),
            applied_gain_db=server_metadata.get("applied_gain_db", 0.0),
            model_type=model_type,
            model_name=model_name,
            transcript_chars=len(text),
            transcript_words=len(text.split()) if text else 0,
            postprocess_profile=postprocess,
        )
        path = write_record(record)
        if path:
            logger.debug(f"Diagnostics written to: {path}")
    except Exception as e:  # noqa: BLE001 — diagnostics must never block dictation
        logger.debug(f"Diagnostics skipped (non-fatal): {e}")


def listen_continuous(
    output_file: str | None = None,
    background: bool = False,
    clipboard: bool = True,
    config_overrides: dict[str, Any] | None = None,
    postprocess: str | None = None,
) -> int:
    """Run continuous dictation: loop transcribing utterances until interrupted.

    When ``postprocess`` is set, the AIPP profile is applied **once at end of
    session** to the concatenated transcript (not per utterance). This is
    deliberate — running an LLM on each utterance loses cross-utterance
    context and multiplies cost N times. The processed result is written
    alongside the raw transcript as ``<name>.processed.txt`` and copied to
    the clipboard. The raw transcript file is kept as the source of truth.
    """
    config = load_app_config()
    if config_overrides:
        config.update(config_overrides)

    pm = ProcessManager("long_dictation")
    _, existing_pid = pm.is_running()
    if existing_pid != os.getpid():
        pm.write_pid(os.getpid())

    stop_event = threading.Event()
    _set_stop_event_on_signal(stop_event)

    _log_threshold_source(
        float(config.get("silence_threshold", CODE_DEFAULTS["silence_threshold"]))
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if output_file:
        transcript_path = Path(output_file).expanduser()
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_filename = transcript_path.name
    else:
        transcript_filename = f"{timestamp}_long.txt"
        transcript_path = get_transcript_dir() / transcript_filename
    transcript_path.touch()

    if background:
        logger.info("Starting long dictation mode (background).")
        _notify("Long dictation started. Run 'talkat toggle-long' to stop.")
    else:
        logger.info("Starting long dictation mode. Press Ctrl+C to stop.")
        _notify("Long dictation mode started. Press Ctrl+C to stop.")
    logger.info(f"Transcript will be saved to: {transcript_path}")
    if clipboard:
        logger.info("Transcript will be copied to clipboard when finished.")

    silence_timeout = float(
        config.get("long_mode_silence_timeout", CODE_DEFAULTS["long_mode_silence_timeout"])
    )
    max_session_duration = float(
        config.get(
            "long_mode_max_session_duration",
            CODE_DEFAULTS["long_mode_max_session_duration"],
        )
    )
    max_consecutive_errors = int(
        config.get(
            "long_mode_max_consecutive_errors",
            CODE_DEFAULTS["long_mode_max_consecutive_errors"],
        )
    )

    # We append each segment straight to disk and never accumulate the full
    # transcript in memory. The final clipboard copy reads the file back, so
    # memory stays bounded regardless of session length.
    session_word_count = 0
    consecutive_errors = 0
    return_code = 0
    session_start = time.monotonic()
    last_speech_at = session_start

    # Diagnostics aggregates across the whole long-mode session — one record
    # per session, not per utterance. Per-utterance would be diagnostics spam.
    diag_audio_duration = 0.0
    diag_asr_seconds = 0.0
    diag_gain_samples: list[float] = []
    diag_errors: list[str] = []

    try:
        with TranscriptionClient(config) as client:
            while not stop_event.is_set():
                now = time.monotonic()
                if now - session_start > max_session_duration:
                    logger.info(
                        f"Reached max session duration "
                        f"({max_session_duration / 60:.0f} min), stopping."
                    )
                    break
                if now - last_speech_at > silence_timeout:
                    logger.info(f"No speech for {silence_timeout:.0f}s, stopping.")
                    break

                # Cap each utterance attempt at the silence timeout so the loop
                # wakes up to re-check session/silence limits between attempts.
                try:
                    text = client.transcribe_one_utterance(
                        stop_event=stop_event,
                        max_duration=silence_timeout,
                        debug=False,
                    )
                    consecutive_errors = 0
                    metadata = getattr(client, "last_metadata", {})
                    diag_audio_duration += metadata.get("audio_duration", 0.0)
                    diag_asr_seconds += metadata.get("asr_seconds", 0.0)
                    gain = metadata.get("applied_gain_db", 0.0)
                    if gain:
                        diag_gain_samples.append(gain)
                except TranscriptionUnreachable as e:
                    logger.error(str(e))
                    _notify("Error: Model server not reachable.")
                    diag_errors.append(f"unreachable: {e}")
                    return_code = 1
                    break
                except TranscriptionServerError as e:
                    logger.error(str(e))
                    if stop_event.is_set():
                        break
                    consecutive_errors += 1
                    diag_errors.append(f"server_error: {e}")
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error(
                            f"Aborting long dictation after "
                            f"{consecutive_errors} consecutive server errors."
                        )
                        _notify(
                            f"Long dictation aborted: "
                            f"{consecutive_errors} consecutive server errors."
                        )
                        return_code = 1
                        break
                    continue
                except AudioSessionError as e:
                    logger.error(f"Audio error: {e}")
                    diag_errors.append(f"audio: {e}")
                    return_code = 1
                    break

                if text:
                    logger.info(f"Recognized: {text}")
                    session_word_count += len(text.split())
                    last_speech_at = time.monotonic()
                    with open(transcript_path, "a", encoding="utf-8") as f:
                        f.write(text + " ")
    except KeyboardInterrupt:
        # Raised from our SIGINT/SIGTERM handler to interrupt blocking I/O.
        logger.info("Long dictation interrupted.")
    except Exception as e:
        logger.error(f"Error in long dictation mode: {e}")
        traceback.print_exc()
        return_code = 1
    finally:
        logger.info("Cleaning up long dictation session...")

        try:
            full_text = transcript_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.error(f"Could not read transcript for final summary: {e}")
            full_text = ""

        if full_text:
            word_count = len(full_text.split()) or session_word_count

            # End-of-session AIPP (if requested). Single LLM call on the full
            # transcript so the model sees the whole context. Side-by-side
            # .processed.txt keeps the raw file intact as source of truth.
            clipboard_text = full_text
            if postprocess:
                from .postprocess import postprocess_text

                processed = postprocess_text(full_text, postprocess, config=config)
                if processed and processed != full_text:
                    processed_path = transcript_path.with_suffix(".processed.txt")
                    try:
                        processed_path.write_text(processed, encoding="utf-8")
                        logger.info(f"Post-processed transcript saved to: {processed_path}")
                        clipboard_text = processed
                    except OSError as e:
                        logger.error(f"Could not write processed transcript: {e}")

            clipboard_ok = clipboard and copy_to_clipboard(clipboard_text)
            if clipboard and not clipboard_ok:
                logger.warning("Could not copy to clipboard (wl-copy or xclip not available)")
            logger.info(f"Full transcript saved to: {transcript_path}")
            logger.info(f"Total words: {word_count}")
            if clipboard_ok:
                _notify(f"Stopped. {word_count} words copied to clipboard.")
            else:
                _notify(f"Stopped. {word_count} words saved to {transcript_filename}.")
        else:
            logger.info("No transcript to save (no speech detected)")
            _notify("Stopped. No speech detected.")

        try:
            socket_path = config.get("server_socket", CODE_DEFAULTS["server_socket"])
            model_type, model_name = _fetch_server_info(socket_path)
            mean_gain = (
                sum(diag_gain_samples) / len(diag_gain_samples) if diag_gain_samples else 0.0
            )
            record = build_record(
                mode="long",
                audio_duration=diag_audio_duration,
                asr_seconds=diag_asr_seconds,
                applied_gain_db=mean_gain,
                model_type=model_type,
                model_name=model_name,
                transcript_chars=len(full_text),
                transcript_words=len(full_text.split()) if full_text else session_word_count,
                postprocess_profile=postprocess,
                errors=diag_errors,
                extra={
                    "session_seconds": round(time.monotonic() - session_start, 3),
                    "utterances_with_gain_boost": len(diag_gain_samples),
                },
            )
            write_record(record)
        except Exception as e:  # noqa: BLE001 — diagnostics never block dictation
            logger.debug(f"Long-mode diagnostics skipped (non-fatal): {e}")

        pm.cleanup_pid_file()

    return return_code
