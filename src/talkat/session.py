"""A dictation session: one open microphone, cut at pauses, transcribed while you talk.

Three threads:

* capture — reads the microphone for the whole session and cuts the stream
  into segments at natural pauses (:class:`~talkat.segmenter.Segmenter`);
* transcription — sends segments to the model server one at a time, in
  order, each with the end of the previous segment's text as context;
* the caller's — :meth:`DictationSession.run` hands each result to
  ``on_result`` the moment it's ready (to type it, append it to a file, ...).

The microphone stays open until the session ends, so nothing said while a
segment is being transcribed is lost. Audio that can't be transcribed — the
server is still failing after the retries, or the session was aborted first —
is saved as a WAV, and a marker pointing at it takes its place in the text.

Stopping is event-driven; signal handlers only set the events. ``stop_event``
ends capture, and everything recorded is still transcribed and delivered.
``abort_event`` stops sending requests: audio not yet transcribed is saved.
"""

import queue
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .client import TranscriptionClient, TranscriptionServerError, TranscriptionUnreachable
from .logging_config import get_logger
from .paths import UNTRANSCRIBED_DIR
from .record import AudioSession, AudioSessionError, StopReason
from .segmenter import SAMPLE_RATE, CutReason, Segment, Segmenter

logger = get_logger(__name__)

# Waits before each retry of a failed segment; together they ride out a
# server restart (~2 s). Once a segment fails for good, later segments get a
# single attempt until one succeeds, so a dead server can't stall the session.
RETRY_DELAYS_S = (0.5, 2.0, 5.0)
# Context sent with a segment: the end of the previous segment's text.
PROMPT_CHARS = 200
# After an abort, how long an in-flight request may still finish before its
# audio is saved instead. Short: an abort is stop_process escalating to
# SIGTERM, and its SIGKILL follows one second later.
ABORT_GRACE_S = 0.5
_POLL_S = 0.1


@dataclass(frozen=True)
class SegmentResult:
    index: int
    # The transcript ("" for silence) — or, if ``failed``, a marker saying
    # where the audio was saved.
    text: str
    seconds: float
    cut: CutReason
    failed: bool = False
    error: str | None = None
    server_metadata: dict[str, float] = field(default_factory=dict)


@dataclass
class SessionOutcome:
    results: list[SegmentResult]
    stop_reason: StopReason | None
    audio_error: AudioSessionError | None = None
    aborted: bool = False
    idle_stopped: bool = False

    @property
    def text(self) -> str:
        return " ".join(r.text for r in self.results if r.text)

    @property
    def failures(self) -> list[SegmentResult]:
        return [r for r in self.results if r.failed]


class DictationSession:
    def __init__(
        self,
        client: TranscriptionClient,
        *,
        threshold: float,
        max_duration: float,
        stop_event: threading.Event,
        abort_event: threading.Event,
        on_recording_started: Callable[[], None] | None = None,
        on_recording_stopped: Callable[[StopReason | None], None] | None = None,
        untranscribed_dir: Path = UNTRANSCRIBED_DIR,
        debug: bool = False,
    ) -> None:
        """``threshold`` is the calibrated speech level the segmenter cuts on."""
        self.client = client
        self.threshold = threshold
        self.max_duration = max_duration
        self.stop_event = stop_event
        self.abort_event = abort_event
        self._on_recording_started = on_recording_started
        self._on_recording_stopped = on_recording_stopped
        self._untranscribed_dir = untranscribed_dir
        self._debug = debug
        self._label = datetime.now().strftime("%Y%m%d_%H%M%S")

        self.stop_reason: StopReason | None = None
        self.audio_error: AudioSessionError | None = None
        # How long the captured audio has been below the threshold; written by
        # the capture thread, read for the "still recording?" reminders.
        self.quiet_seconds = 0.0
        self._segments: queue.Queue[tuple[int, Segment] | None] = queue.Queue()
        self._results: queue.Queue[SegmentResult | None] = queue.Queue()
        # Recorded segments without a published result. Guarded by _lock, which
        # also covers publishing, so an abort can take over stranded audio
        # without racing the transcription thread.
        self._pending: dict[int, Segment] = {}
        self._next_index = 0
        self._lock = threading.Lock()

    def run(
        self,
        on_result: Callable[[SegmentResult], None],
        idle_timeout: float | None = None,
        idle_notify_interval: float | None = None,
        on_idle: Callable[[float], None] | None = None,
    ) -> SessionOutcome:
        """Record until stopped; call ``on_result`` for each segment, in order.

        ``idle_timeout`` ends the session once no speech has been
        *transcribed* for that many seconds — text, so that a noisy room
        can't keep a forgotten session alive. ``on_idle`` is called every
        ``idle_notify_interval`` that the *audio* stays below the threshold,
        so a quiet session doesn't look dead and a long sentence still being
        transcribed isn't mistaken for silence.
        """
        capture = threading.Thread(target=self._capture, name="talkat-capture", daemon=True)
        worker = threading.Thread(target=self._transcribe_all, name="talkat-asr", daemon=True)
        capture.start()
        worker.start()

        results: list[SegmentResult] = []
        last_speech = time.monotonic()
        last_idle_notice = last_speech
        idle_stopped = False
        abort_deadline: float | None = None
        while True:
            now = time.monotonic()
            if self.abort_event.is_set():
                abort_deadline = abort_deadline or now + ABORT_GRACE_S
                if now >= abort_deadline:
                    capture.join(timeout=0.2)  # stop_event is set; capture is flushing
                    for last in self._give_up_on_pending():
                        results.append(last)
                        on_result(last)
                    break
            elif (
                idle_timeout is not None
                and not self.stop_event.is_set()
                and not self._transcribing()
                and now - last_speech > idle_timeout
            ):
                logger.info(f"No speech for {idle_timeout:.0f}s, stopping.")
                idle_stopped = True
                self.stop_event.set()
            elif (
                idle_notify_interval
                and on_idle is not None
                and not self.stop_event.is_set()
                and not self._transcribing()
                and self.quiet_seconds >= idle_notify_interval
                and now - last_idle_notice >= idle_notify_interval
            ):
                last_idle_notice = now
                on_idle(self.quiet_seconds)

            try:
                result = self._results.get(timeout=_POLL_S)
            except queue.Empty:
                continue
            if result is None:
                break
            if result.text and not result.failed:
                last_speech = time.monotonic()
            results.append(result)
            on_result(result)

        return SessionOutcome(
            results=results,
            stop_reason=self.stop_reason,
            audio_error=self.audio_error,
            aborted=self.abort_event.is_set(),
            idle_stopped=idle_stopped,
        )

    def _transcribing(self) -> bool:
        """Is any captured audio still waiting for its text?"""
        with self._lock:
            return bool(self._pending)

    # -- capture thread ---------------------------------------------------

    def _capture(self) -> None:
        segmenter = Segmenter(self.threshold)
        try:
            with AudioSession(
                max_duration=self.max_duration,
                stop_event=self.stop_event,
                debug=self._debug,
            ) as audio:
                if self._on_recording_started is not None:
                    self._on_recording_started()
                for chunk in audio:
                    segment = segmenter.feed(chunk)
                    self.quiet_seconds = segmenter.quiet_seconds
                    if segment is not None:
                        self._enqueue(segment)
                self.stop_reason = audio.stop_reason
        except AudioSessionError as e:
            self.audio_error = e  # the microphone never opened
        except Exception:
            logger.exception("Capture stopped unexpectedly")
            self.stop_reason = "read_error"
        finally:
            tail = segmenter.flush()
            if tail is not None:
                self._enqueue(tail)
            self._segments.put(None)
        if self.audio_error is None and self._on_recording_stopped is not None:
            self._on_recording_stopped(self.stop_reason)

    def _enqueue(self, segment: Segment) -> None:
        with self._lock:
            index = self._next_index
            self._next_index += 1
            self._pending[index] = segment
        logger.debug(f"Segment {index}: {segment.seconds:.1f}s, cut at {segment.cut}")
        self._segments.put((index, segment))

    # -- transcription thread ---------------------------------------------

    def _transcribe_all(self) -> None:
        prompt: str | None = None
        server_failing = False
        while (item := self._segments.get()) is not None:
            index, segment = item
            if self.abort_event.is_set():
                self._fail(index, segment, "stopped before it was transcribed")
                continue
            retries = 0 if server_failing else len(RETRY_DELAYS_S)
            text, error = self._transcribe(segment, prompt, retries)
            if text is None:
                server_failing = True
                prompt = None
                self._fail(index, segment, error)
                continue
            server_failing = False
            # No context after silence: a prompt over near-silent audio is
            # what makes Whisper echo it back.
            prompt = text[-PROMPT_CHARS:] if text else None
            self._publish(
                index,
                SegmentResult(
                    index=index,
                    text=text,
                    seconds=segment.seconds,
                    cut=segment.cut,
                    server_metadata=dict(self.client.last_metadata),
                ),
            )
        with self._lock:
            self._results.put(None)

    def _transcribe(
        self, segment: Segment, prompt: str | None, retries: int
    ) -> tuple[str | None, str | None]:
        """Returns (text, None) on success, (None, why) once it gives up."""
        error: str | None = None
        for attempt in range(retries + 1):
            if attempt and self.abort_event.wait(RETRY_DELAYS_S[attempt - 1]):
                return None, "stopped before it was transcribed"
            try:
                return self.client.transcribe_audio(segment.pcm, prompt=prompt), None
            except (TranscriptionUnreachable, TranscriptionServerError) as e:
                error = str(e)
                logger.warning(f"Transcription attempt {attempt + 1}/{retries + 1} failed: {e}")
        return None, error

    def _publish(self, index: int, result: SegmentResult) -> None:
        with self._lock:
            if self._pending.pop(index, None) is None:
                return  # an abort already saved this segment's audio
            self._results.put(result)

    def _fail(self, index: int, segment: Segment, error: str | None) -> None:
        with self._lock:
            if index not in self._pending:
                return
        self._publish(index, self._saved_audio_result(index, segment, error))

    # -- untranscribed audio ----------------------------------------------

    def _give_up_on_pending(self) -> list[SegmentResult]:
        """Abort grace expired: collect published results, save the rest's audio."""
        with self._lock:
            done: list[SegmentResult] = []
            while True:
                try:
                    item = self._results.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    done.append(item)
            stranded = sorted(self._pending.items())
            self._pending.clear()
        return done + [
            self._saved_audio_result(index, segment, "stopped before it was transcribed")
            for index, segment in stranded
        ]

    def _saved_audio_result(self, index: int, segment: Segment, error: str | None) -> SegmentResult:
        path = self._save_audio(index, segment.pcm)
        marker = (
            f"[untranscribed audio: talkat file {path}]"
            if path is not None
            else "[untranscribed audio: could not be saved]"
        )
        return SegmentResult(
            index=index,
            text=marker,
            seconds=segment.seconds,
            cut=segment.cut,
            failed=True,
            error=error,
        )

    def _save_audio(self, index: int, pcm: bytes) -> Path | None:
        path = self._untranscribed_dir / f"{self._label}_{index:03d}.wav"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(SAMPLE_RATE)
                wav.writeframes(pcm)
        except OSError as e:
            logger.error(f"Could not save untranscribed audio to {path}: {e}")
            return None
        logger.warning(f"Saved untranscribed audio to {path}")
        return path
