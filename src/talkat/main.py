#!/usr/bin/env python3

import contextlib
import json
import os
import signal
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any

import httpx

from .client import TranscriptionClient
from .clipboard import copy_to_clipboard
from .config import CODE_DEFAULTS, ConfigFileError, load_app_config, update_user_config
from .diagnostics import build_record, write_record
from .focus import get_focused_window
from .keyboard import ModifierWatch
from .logging_config import get_logger
from .paths import TRANSCRIPT_DIR
from .process_manager import ProcessManager
from .record import StopReason, calibrate_microphone
from .security import safe_subprocess_run, sanitize_text_for_typing
from .session import DictationSession, SegmentResult, SessionOutcome

logger = get_logger(__name__)

# How long typing waits for held modifier keys to be released before handing
# the rest of the transcript to the clipboard: long enough to ride out a
# shortcut pressed mid-typing, short enough that a stuck key can't stall
# delivery.
MODIFIER_RELEASE_TIMEOUT_S = 5.0

UNTRANSCRIBED_NOTICE = "Part of the recording couldn't be transcribed (audio saved)"


def get_transcript_dir() -> Path:
    """Get or create the transcript directory."""
    config = load_app_config()
    transcript_dir_str = config.get("transcript_dir", str(TRANSCRIPT_DIR))
    transcript_dir = Path(os.path.expanduser(transcript_dir_str))
    transcript_dir.mkdir(parents=True, exist_ok=True)
    return transcript_dir


def save_transcript(text: str, mode: str = "dictation") -> Path:
    """Save transcript to a file with timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{mode}.txt"
    filepath = get_transcript_dir() / filename

    with open(filepath, "a", encoding="utf-8") as f:
        f.write(text + "\n")

    return filepath


def _format_duration(seconds: float) -> str:
    """Whole minutes read better in a toast; anything else stays in seconds."""
    if seconds >= 60 and seconds % 60 < 0.5:
        return f"{seconds / 60:g} min"
    return f"{seconds:.0f}s"


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


def _set_stop_event_on_signal(stop_event: threading.Event, abort_event: threading.Event) -> None:
    """
    Install signal handlers for a graceful-then-forceful stop. Neither raises.

    **First signal** (the toggle's SIGINT): set ``stop_event``. Capture ends
    within one ~30 ms chunk, and everything recorded is still transcribed and
    delivered. Raising here instead was the v1.0.0 toggle regression: every
    hotkey stop tore down the request in flight and typed nothing.

    **Second signal** (``stop_process`` escalating to SIGTERM after
    ``process_stop_timeout``, or Ctrl+C twice in a terminal — the stop hotkey
    can't send one, its first press holds the lock while it waits): set
    ``abort_event``. Typing stops between keystrokes, no new transcription
    requests start, and audio not yet transcribed is saved (see
    ``session.py``) — fast, since ``stop_process`` sends SIGKILL a second
    later. This used to raise ``KeyboardInterrupt`` as an escape from a hung
    server wait; the main thread no longer blocks on the server, and a raise
    inside ``subprocess.run`` SIGKILLs ydotool mid-keystroke, leaving the key
    held down.

    The handler does NO logging or cleanup — Python's logging module isn't
    async-signal-safe and can deadlock if invoked from a handler.
    """

    def handler(signum: int, frame: FrameType | None) -> None:
        if stop_event.is_set():
            abort_event.set()
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

    try:
        update_user_config({"silence_threshold": threshold})
    except ConfigFileError as e:
        # The measurement is still good — say it, so it can be set by hand.
        logger.error(f"Calibration measured {threshold:.1f}, but it wasn't saved: {e}")
        logger.error(f'Fix the file, or set "silence_threshold": {threshold:.1f} by hand.')
        _notify(f"Calibration threshold {threshold:.1f} not saved — config file unreadable.")
        return 1

    logger.info(f"Calibration complete. Threshold set to: {threshold:.1f}")
    _notify(f"Calibration complete. Threshold: {threshold:.1f}")
    return 0


def run_dictation(
    output_file: str | None = None,
    to_file: bool = False,
    config_overrides: dict[str, Any] | None = None,
    postprocess: str | None = None,
) -> int:
    """Record until stopped and deliver the text — the one dictation route.

    `talkat listen` toggles it: the first invocation records, the second stops
    that one. cli.py owns the lock and the PID file; we only clean up on exit.
    The recording is cut at natural pauses and each piece is transcribed in
    the background while the microphone stays open (see ``session.py``).

    Delivery:

    * by default each piece is typed as soon as it's transcribed;
    * with ``to_file``, pieces are appended to the transcript file as they
      arrive and the whole transcript goes to the clipboard at the end,
      typing nothing (long-form note taking);
    * ``output_file`` on its own, ``postprocess`` (AIPP rewrites the whole
      transcript), and ``output_mode: clipboard`` deliver once, at the end.

    Recording ends when stopped, after ``idle_timeout`` without transcribed
    speech (with a reminder every ``idle_notify_interval`` while it's quiet),
    at ``max_recording_duration``, or once ``max_consecutive_errors`` pieces
    in a row can't be transcribed.

    AIPP is fail-open — a misconfigured profile or unreachable LLM falls back
    to the raw transcript with a notification, so the dictation is never lost.
    """
    config = load_app_config()
    if config_overrides:
        config.update(config_overrides)

    pm = ProcessManager("listen")

    stop_event = threading.Event()
    abort_event = threading.Event()
    _set_stop_event_on_signal(stop_event, abort_event)

    threshold = float(config.get("silence_threshold", CODE_DEFAULTS["silence_threshold"]))
    _log_threshold_source(threshold)

    max_recording_duration = float(
        config.get("max_recording_duration", CODE_DEFAULTS["max_recording_duration"])
    )
    idle_timeout = float(config.get("idle_timeout", CODE_DEFAULTS["idle_timeout"]))
    idle_notify_interval = float(
        config.get("idle_notify_interval", CODE_DEFAULTS["idle_notify_interval"])
    )
    max_consecutive_errors = int(
        config.get("max_consecutive_errors", CODE_DEFAULTS["max_consecutive_errors"])
    )
    output_mode = config.get("output_mode", CODE_DEFAULTS["output_mode"])
    types_text = output_mode == "type" and not to_file and not output_file

    # The focus guard compares against the window focused at invocation time —
    # that's the window the user intends to dictate into.
    focus_before: str | None = None
    if types_text and config.get("focus_guard", CODE_DEFAULTS["focus_guard"]):
        focus_before = get_focused_window()

    # Type each piece the moment it's transcribed, unless the transcript has
    # to be whole first (AIPP rewrites all of it).
    typist = _Typist(focus_before, abort_event) if types_text and not postprocess else None

    transcript_file: _TranscriptFile | None = None
    if to_file:
        transcript_file = _TranscriptFile(_transcript_path(output_file))
        logger.info(f"Transcript will be saved to: {transcript_file.path}")

    def _announce_recording() -> None:
        logger.info("Recording — speak now. (Run 'talkat listen' again to stop.)")
        _notify('Recording... Run "talkat listen" again to stop')

    def _announce_auto_stop(reason: StopReason | None) -> None:
        # A recording that ends on its own must say so at once: someone who
        # thinks they're still recording keeps talking, then reaches for the
        # stop hotkey while the transcript is being typed.
        if reason == "max_duration":
            limit = _format_duration(max_recording_duration)
            _notify(f"Recording hit the {limit} limit — transcribing.")
        elif reason == "read_error":
            _notify("Microphone error — transcribing what was recorded.")

    def _announce_idle(idle_seconds: float) -> None:
        # Silence is not a stop: say so, or a session left open looks dead.
        quiet = _format_duration(idle_seconds)
        logger.info(f"No speech for {quiet} — still recording.")
        _notify(f'No speech for {quiet} — still dictating. Run "talkat listen" again to stop.')

    failure_announced = False
    consecutive_failures = 0

    def on_result(result: SegmentResult) -> None:
        nonlocal failure_announced, consecutive_failures
        if result.failed:
            consecutive_failures += 1
            if not failure_announced:
                failure_announced = True
                where = (
                    "the rest will go to the clipboard"
                    if typist is not None
                    else "see the transcript for where"
                )
                _notify(f"{UNTRANSCRIBED_NOTICE} — {where}.")
            if consecutive_failures == max_consecutive_errors:
                logger.error(
                    f"Stopping: {consecutive_failures} pieces in a row couldn't be transcribed."
                )
                _notify(f"Dictation stopped: {consecutive_failures} transcription failures.")
                stop_event.set()
        else:
            consecutive_failures = 0
            if result.text:
                logger.info(f"Recognized: {result.text}")
        if transcript_file is not None:
            transcript_file.add(result.text)
        if typist is not None:
            typist.add(result.text, transcribed=not result.failed)

    session_start = time.monotonic()
    with TranscriptionClient(config) as client:
        outcome = DictationSession(
            client,
            threshold=threshold,
            max_duration=max_recording_duration,
            stop_event=stop_event,
            abort_event=abort_event,
            on_recording_started=_announce_recording,
            on_recording_stopped=_announce_auto_stop,
            debug=True,
        ).run(
            on_result,
            idle_timeout=idle_timeout,
            idle_notify_interval=idle_notify_interval,
            on_idle=_announce_idle,
        )

    if outcome.audio_error is not None:
        logger.error(str(outcome.audio_error))
        _notify(f"Audio error: {outcome.audio_error}")
        pm.cleanup_pid_file()
        return 1
    if outcome.idle_stopped:
        _notify(f"Stopped: no speech for {_format_duration(idle_timeout)}.")

    complete = not outcome.failures
    if transcript_file is not None:
        text = _finish_transcript_file(transcript_file, config, postprocess, complete)
    else:
        text = outcome.text
        if not text:
            logger.warning("No text recognized in the audio")
            _notify("No text recognized")
            _write_session_diagnostics(config, "", outcome, postprocess, session_start)
            pm.cleanup_pid_file()
            return 0

        logger.info(f"Recognized: {text}")
        if config.get("save_transcripts", True):
            logger.info(f"Transcript saved to: {save_transcript(text)}")

        if typist is not None:
            typist.finish()
        else:
            if postprocess and not complete:
                # The LLM would rewrite the saved-audio markers.
                logger.warning(f"Skipping post-processing: {UNTRANSCRIBED_NOTICE.lower()}.")
            elif postprocess:
                from .postprocess import postprocess_text

                text = postprocess_text(text, postprocess, config=config)
            if output_file:
                output_path = Path(output_file).expanduser()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(text, encoding="utf-8")
                logger.info(f"Transcription saved to: {output_path}")
                _notify(f"Saved to: {output_path.name}")
            else:
                _deliver_text(text, config, focus_before, abort_event, complete=complete)

    _write_session_diagnostics(config, text, outcome, postprocess, session_start)
    pm.cleanup_pid_file()
    return 0 if complete else 1


def _transcript_path(output_file: str | None) -> Path:
    """Where --to-file appends: the given path, else a timestamped transcript."""
    if output_file:
        path = Path(output_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        path = get_transcript_dir() / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_dictation.txt"
    path.touch()
    return path


class _TranscriptFile:
    """Appends each piece to a transcript file as it arrives; nothing is typed.

    The file is the source of truth — it never accumulates in memory, so a
    session's length doesn't bound what we can record.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, piece: str) -> None:
        if not piece:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(piece + " ")

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.error(f"Could not read transcript: {e}")
            return ""


def _finish_transcript_file(
    transcript_file: _TranscriptFile,
    config: dict[str, Any],
    postprocess: str | None,
    complete: bool,
) -> str:
    """Wrap up a --to-file session: optional AIPP, clipboard, summary."""
    full_text = transcript_file.read()
    if not full_text:
        logger.info("No transcript to save (no speech detected)")
        _notify("Stopped. No speech detected.")
        return ""

    # End-of-session AIPP. Single LLM call on the full transcript so the model
    # sees the whole context; the side-by-side .processed.txt keeps the raw
    # file intact as the source of truth.
    clipboard_text = full_text
    if postprocess and not complete:
        logger.warning(f"Skipping post-processing: {UNTRANSCRIBED_NOTICE.lower()}.")
    elif postprocess:
        from .postprocess import postprocess_text

        processed = postprocess_text(full_text, postprocess, config=config)
        if processed and processed != full_text:
            processed_path = transcript_file.path.with_suffix(".processed.txt")
            try:
                processed_path.write_text(processed, encoding="utf-8")
                logger.info(f"Post-processed transcript saved to: {processed_path}")
                clipboard_text = processed
            except OSError as e:
                logger.error(f"Could not write processed transcript: {e}")

    words = len(full_text.split())
    logger.info(f"Full transcript saved to: {transcript_file.path} ({words} words)")
    if copy_to_clipboard(clipboard_text):
        _notify(f"Stopped. {words} words copied to clipboard.")
    else:
        logger.warning("Could not copy to clipboard (wl-copy or xclip not available)")
        _notify(f"Stopped. {words} words saved to {transcript_file.path.name}.")
    return full_text


def _deliver_text(
    text: str,
    config: dict[str, Any],
    focus_before: str | None,
    abort_event: threading.Event | None = None,
    *,
    complete: bool = True,
) -> None:
    """Deliver a whole transcript at once: type it into the focused window, else clipboard.

    The invariant: the transcript is never silently lost. Whatever can't be
    typed lands in the clipboard or, failing that, on stdout — with a
    notification saying where it went. ``complete=False`` (part of the
    recording couldn't be transcribed) is never typed: it goes to the
    clipboard with its saved-audio markers.
    """
    if config.get("output_mode", CODE_DEFAULTS["output_mode"]) == "clipboard":
        if copy_to_clipboard(text):
            logger.info(f"Copied to clipboard: {text}")
            if complete:
                _notify(f"Copied: {text[:100]}")
            else:
                _notify(f"{UNTRANSCRIBED_NOTICE} — transcript copied to clipboard.")
        else:
            logger.warning("Clipboard unavailable (wl-copy/xclip missing), printing instead:")
            print(f"TEXT: {text}")
            _notify(f"Recognized: {text[:100]}")
        return

    typist = _Typist(focus_before, abort_event or threading.Event())
    typist.add(text, transcribed=complete)
    typist.finish()


class _Typist:
    """Types a transcript into the focused window, piece by piece as it arrives.

    Once typing has to stop — focus moved, a modifier key stayed held, a
    second stop, ydotool failed, or a piece couldn't be transcribed — nothing
    more is typed, and :meth:`finish` puts everything untyped on the clipboard.
    """

    def __init__(self, focus_before: str | None, abort_event: threading.Event) -> None:
        self._focus_before = focus_before
        self._abort_event = abort_event
        self._typed = ""
        self._untyped = ""
        self.stop_reason: str | None = None

    def add(self, piece: str, transcribed: bool = True) -> None:
        # No length cap: the recording limit already bounds a transcript, and
        # sanitize's default cap would silently drop the end of a long one.
        piece = sanitize_text_for_typing(piece, max_length=len(piece)).strip()
        if not piece:
            return
        if self._typed or self._untyped:
            piece = " " + piece
        if not transcribed and self.stop_reason is None:
            # Typing on past a gap would leave text with a hole in it.
            self.stop_reason = UNTRANSCRIBED_NOTICE
        if self.stop_reason is not None:
            self._untyped += piece
            return
        typed, reason = _type_text(piece, self._focus_before, self._abort_event)
        self._typed += piece[:typed]
        if reason is not None:
            self.stop_reason = reason
            self._untyped += piece[typed:]

    def finish(self) -> None:
        """Report the result; put whatever wasn't typed on the clipboard."""
        # Pasted right after typed text, the untyped part needs its leading space.
        rest = self._untyped if self._typed else self._untyped.lstrip()
        if not rest.strip():
            logger.info(f"Typed: {self._typed}")
            _notify(f"Typed: {self._typed[:100]}")
            return

        reason = self.stop_reason or "typing stopped"
        logger.warning(f"Typing stopped after {len(self._typed)} characters: {reason}.")
        what = "the rest" if self._typed else "the transcript"
        headline = reason[:1].upper() + reason[1:]
        if copy_to_clipboard(rest):
            _notify(f"{headline} — {what} copied to clipboard.")
        else:
            print(f"TEXT: {rest}")
            _notify(f"{headline} — {what} printed to the console (no clipboard tool).")


def _type_text(
    text: str, focus_before: str | None, abort_event: threading.Event
) -> tuple[int, str | None]:
    """Type ``text`` with one ydotool call per keystroke.

    Returns how many characters were consumed and, if typing stopped early,
    why (``None`` once all of it is typed).

    One call per character is what lets the modifier guard work. ydotoold's
    virtual keyboard shares the seat's modifier state, so while Super is
    physically held every typed letter is a compositor shortcut — on niri,
    typing " off of the tool if they" with Super down opened six launchers
    and three terminals. Checking before each keystroke confines a badly
    timed press to the one key already in flight.

    Focus is re-checked at each word boundary and after any modifier wait
    (the shortcut may have moved it). Both sides must be known to conclude
    "changed" — an IPC failure disables the guard, not typing.
    """
    with ModifierWatch() as modifiers:
        check_focus = True
        for i, char in enumerate(text):
            if abort_event.is_set():
                return i, "stopped"
            if modifiers.held():
                logger.info("Modifier key held — typing paused until it's released.")
                if not modifiers.wait_released(MODIFIER_RELEASE_TIMEOUT_S):
                    return i, "a modifier key stayed held"
                check_focus = True
            if check_focus and focus_before is not None:
                focus_now = get_focused_window()
                if focus_now is not None and focus_now != focus_before:
                    logger.warning(
                        f"Focused window changed during dictation ({focus_before} -> {focus_now})."
                    )
                    return i, "focus changed"
            check_focus = char.isspace()
            if not char.isascii():
                # ydotool 1.0.x indexes its ASCII keymap with a signed char,
                # so any other byte reads outside the table. Never send one.
                continue
            try:
                # --escape=0: typed on its own, "\" would start an escape
                # sequence and never appear.
                safe_subprocess_run(["ydotool", "type", "--escape=0", "--", char], check=True)
            except Exception as e:
                # ydotool missing, ydotoold not running, a crash mid-type.
                logger.warning(f"Typing failed: {e}")
                return i, "typing failed (ydotool error)"
    return len(text), None


def _write_session_diagnostics(
    config: dict[str, Any],
    text: str,
    outcome: SessionOutcome | None,
    postprocess: str | None,
    session_start: float,
) -> None:
    """Build + write one diagnostics record for a dictation session.

    Diagnostics are advisory: any failure inside is swallowed so the user
    never loses dictation because a JSON write hit ENOSPC or similar.
    """
    try:
        results = outcome.results if outcome is not None else []
        transcribed = [r for r in results if not r.failed]
        gains = [
            r.server_metadata["applied_gain_db"]
            for r in transcribed
            if r.server_metadata.get("applied_gain_db")
        ]
        socket_path = config.get("server_socket", CODE_DEFAULTS["server_socket"])
        model_type, model_name = _fetch_server_info(socket_path)
        record = build_record(
            mode="dictation",
            audio_duration=sum(r.server_metadata.get("audio_duration", 0.0) for r in transcribed),
            asr_seconds=sum(r.server_metadata.get("asr_seconds", 0.0) for r in transcribed),
            applied_gain_db=sum(gains) / len(gains) if gains else 0.0,
            model_type=model_type,
            model_name=model_name,
            transcript_chars=len(text),
            transcript_words=len(text.split()),
            postprocess_profile=postprocess,
            errors=[f"segment {r.index}: {r.error}" for r in results if r.failed],
            extra={
                "recording_seconds": round(sum(r.seconds for r in results), 3),
                "segments": len(results),
                "untranscribed_segments": len(results) - len(transcribed),
                "cuts": dict(Counter(r.cut for r in results)),
                "stop_reason": outcome.stop_reason if outcome is not None else None,
                "aborted": outcome.aborted if outcome is not None else False,
                "idle_stopped": outcome.idle_stopped if outcome is not None else False,
                "session_seconds": round(time.monotonic() - session_start, 3),
            },
        )
        path = write_record(record)
        if path:
            logger.debug(f"Diagnostics written to: {path}")
    except Exception as e:  # noqa: BLE001 — diagnostics must never block dictation
        logger.debug(f"Diagnostics skipped (non-fatal): {e}")
