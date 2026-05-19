#!/usr/bin/env python3

import contextlib
import json
import os
import signal
import subprocess
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from .config import CODE_DEFAULTS, load_app_config, save_app_config
from .logging_config import get_logger
from .paths import TRANSCRIPT_DIR
from .process_manager import ProcessManager
from .record import AudioSession, AudioSessionError, calibrate_microphone
from .security import (
    safe_subprocess_run,
    sanitize_text_for_clipboard,
    sanitize_text_for_typing,
)

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


def copy_to_clipboard(text: str) -> bool:
    """Copy text to clipboard using wl-copy (Wayland) or xclip (X11)."""
    text = sanitize_text_for_clipboard(text)

    try:
        safe_subprocess_run(["wl-copy"], input=text.encode("utf-8"), check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    try:
        safe_subprocess_run(
            ["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return False


def _notify(message: str) -> None:
    """Send a desktop notification, ignoring failure (notify-send may be missing)."""
    with contextlib.suppress(FileNotFoundError):
        safe_subprocess_run(["notify-send", "Talkat", message], check=False)


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
        self.server_url: str = config.get("server_url", CODE_DEFAULTS["server_url"])
        self.http_timeout: int = int(config.get("http_timeout", CODE_DEFAULTS["http_timeout"]))
        self.threshold: float = float(
            config.get("silence_threshold", CODE_DEFAULTS["silence_threshold"])
        )
        self.silence_duration: float = float(
            config.get("silence_duration", CODE_DEFAULTS["silence_duration"])
        )
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "TranscriptionClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def transcribe_one_utterance(
        self,
        stop_event: threading.Event | None = None,
        max_duration: float | None = None,
        debug: bool = False,
    ) -> str:
        """
        Record one utterance and return its transcription.

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
            metadata = {"rate": session.sample_rate}
            stream_url = f"{self.server_url}/transcribe_stream"

            def body():
                yield json.dumps(metadata).encode("utf-8") + b"\n"
                for chunk in session:
                    if chunk:
                        yield chunk

            try:
                response = self._session.post(stream_url, data=body(), timeout=self.http_timeout)
                response.raise_for_status()
            except requests.exceptions.ConnectionError as e:
                raise TranscriptionUnreachable(
                    f"Could not connect to the model server at {stream_url}. "
                    "Please ensure the model server is running: talkat server"
                ) from e
            except requests.exceptions.Timeout as e:
                raise TranscriptionUnreachable(f"Request to model server timed out: {e}") from e
            except requests.exceptions.RequestException as e:
                raise TranscriptionServerError(f"Error communicating with model server: {e}") from e

            try:
                return str(response.json().get("text", "")).strip()
            except json.JSONDecodeError as e:
                raise TranscriptionServerError(
                    f"Could not decode JSON response from server: {response.text}"
                ) from e


def _set_stop_event_on_signal(stop_event: threading.Event) -> None:
    """
    Install signal handlers that only set an Event.

    The handler does NO logging or cleanup — Python's logging module isn't
    async-signal-safe and can deadlock if invoked from a handler. The main
    loop observes the event and runs logging/cleanup itself.
    """

    def handler(signum, frame):
        stop_event.set()

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


def listen_once(output_file: str | None = None) -> int:
    """Record one utterance and either type it (default) or save it to a file."""
    config = load_app_config()

    pm = ProcessManager("listen")
    pm.write_pid(os.getpid())

    stop_event = threading.Event()
    _set_stop_event_on_signal(stop_event)

    _log_threshold_source(
        float(config.get("silence_threshold", CODE_DEFAULTS["silence_threshold"]))
    )

    _notify('Recording... Run "talkat listen" again to stop')
    logger.info("Speech detected. Streaming audio to model server...")
    logger.info("(Run 'talkat listen' again to stop recording)")

    try:
        with TranscriptionClient(config) as client:
            text = client.transcribe_one_utterance(stop_event=stop_event, debug=True)
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

    if output_file:
        output_path = Path(output_file).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
        logger.info(f"Transcription saved to: {output_path}")
        _notify(f"Saved to: {output_path.name}")
    else:
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
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.warning("ydotool not available, printing text instead:")
            print(f"TEXT: {text}")
            _notify(f"Recognized: {text[:100]}")

    pm.cleanup_pid_file()
    return 0


def listen_continuous(
    output_file: str | None = None,
    background: bool = False,
    clipboard: bool = True,
) -> int:
    """Run continuous dictation: loop transcribing utterances until interrupted."""
    config = load_app_config()

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

    long_mode_max = float(
        config.get("long_mode_max_duration", CODE_DEFAULTS["long_mode_max_duration"])
    )

    full_transcript: list[str] = []
    return_code = 0

    try:
        with TranscriptionClient(config) as client:
            while not stop_event.is_set():
                try:
                    text = client.transcribe_one_utterance(
                        stop_event=stop_event,
                        max_duration=long_mode_max,
                        debug=False,
                    )
                except TranscriptionUnreachable as e:
                    logger.error(str(e))
                    _notify("Error: Model server not reachable.")
                    return_code = 1
                    break
                except TranscriptionServerError as e:
                    logger.error(str(e))
                    if stop_event.is_set():
                        break
                    continue
                except AudioSessionError as e:
                    logger.error(f"Audio error: {e}")
                    return_code = 1
                    break

                if text:
                    logger.info(f"Recognized: {text}")
                    full_transcript.append(text)
                    with open(transcript_path, "a", encoding="utf-8") as f:
                        f.write(text + " ")
    except Exception as e:
        logger.error(f"Error in long dictation mode: {e}")
        traceback.print_exc()
        return_code = 1
    finally:
        logger.info("Cleaning up long dictation session...")

        full_text = " ".join(full_transcript)
        if full_text:
            if clipboard:
                if copy_to_clipboard(full_text):
                    logger.info("Transcript copied to clipboard!")
                    _notify("Transcript copied to clipboard")
                else:
                    logger.warning("Could not copy to clipboard (wl-copy or xclip not available)")

            logger.info(f"Full transcript saved to: {transcript_path}")
            logger.info(f"Total words: {len(full_text.split())}")
            _notify(f"Long dictation stopped. Saved to {transcript_filename}")
        else:
            logger.info("No transcript to save (no speech detected)")

        pm.cleanup_pid_file()

    return return_code
