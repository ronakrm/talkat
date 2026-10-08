"""Tests for talkat.session.DictationSession — one open mic, ordered background transcription.

The microphone is a scripted fake stream and the model server a fake client;
the threads, queues, retries, and saved-audio paths are the real ones.
"""

from __future__ import annotations

import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from talkat import session as session_mod
from talkat.client import TranscriptionServerError, TranscriptionUnreachable
from talkat.session import DictationSession, SegmentResult, SessionOutcome

CHUNK_SAMPLES = 480  # 30 ms at 16 kHz
_SILENT_CHUNK = np.zeros(CHUNK_SAMPLES, dtype=np.int16).tobytes()


def speech(seconds: float, level: int = 5000) -> list[bytes]:
    return [np.full(CHUNK_SAMPLES, level, dtype=np.int16).tobytes()] * round(seconds / 0.03)


def silence(seconds: float) -> list[bytes]:
    return [_SILENT_CHUNK] * round(seconds / 0.03)


class ScriptedStream:
    """pyaudio.Stream stand-in: plays the scripted chunks, then silence forever."""

    def __init__(self, chunks: list[bytes], pace_s: float) -> None:
        self.chunks = list(chunks)
        self.pace_s = pace_s

    def read(self, n_samples: int, exception_on_overflow: bool = False) -> bytes:
        if self.pace_s:
            time.sleep(self.pace_s)
        return self.chunks.pop(0) if self.chunks else _SILENT_CHUNK

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def mic(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What the fake microphone plays: ``chunks``, read ``pace_s`` apart."""
    from talkat import record as record_mod

    state: dict[str, Any] = {"chunks": [], "pace_s": 0.0, "available": True}

    class FakePyAudio:
        def open(self, **_kwargs: object) -> ScriptedStream:
            return ScriptedStream(state["chunks"], state["pace_s"])

        def terminate(self) -> None:
            pass

    monkeypatch.setattr(record_mod.pyaudio, "PyAudio", FakePyAudio)
    monkeypatch.setattr(
        record_mod,
        "find_microphone",
        lambda p, preferred_name=None: 0 if state["available"] else None,
    )
    monkeypatch.setattr(session_mod, "RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    return state


class FakeClient:
    """TranscriptionClient stand-in: answers from a script, records every request.

    A response is a str, an Exception to raise, or a callable returning a str.
    """

    def __init__(self, responses: list[str | Exception | Callable[[], str]] | None = None):
        self.responses = list(responses or [])
        self.calls: list[tuple[bytes, str | None]] = []
        self.last_metadata: dict[str, float] = {}

    def transcribe_audio(self, pcm: bytes, prompt: str | None = None) -> str:
        self.calls.append((pcm, prompt))
        response = self.responses.pop(0) if self.responses else ""
        if isinstance(response, Exception):
            raise response
        text = response() if callable(response) else response
        self.last_metadata = {"audio_duration": len(pcm) / 32000, "asr_seconds": 0.01}
        return text


def _record(
    mic: dict[str, Any],
    client: FakeClient,
    tmp_path: Path,
    chunks: list[bytes],
    **kwargs: Any,
) -> tuple[SessionOutcome, list[SegmentResult]]:
    """Play ``chunks`` through a session; capture ends when they run out."""
    mic["chunks"] = chunks
    idle_timeout = kwargs.pop("idle_timeout", None)
    idle_notify_interval = kwargs.pop("idle_notify_interval", None)
    on_idle = kwargs.pop("on_idle", None)
    session = DictationSession(
        client,  # type: ignore[arg-type]
        threshold=200.0,
        max_duration=kwargs.pop("max_duration", (len(chunks) + 0.5) * 0.03),
        stop_event=kwargs.pop("stop_event", threading.Event()),
        abort_event=kwargs.pop("abort_event", threading.Event()),
        untranscribed_dir=tmp_path,
        **kwargs,
    )
    delivered: list[SegmentResult] = []
    outcome = session.run(
        delivered.append,
        idle_timeout=idle_timeout,
        idle_notify_interval=idle_notify_interval,
        on_idle=on_idle,
    )
    return outcome, delivered


THREE_SENTENCES = speech(5) + silence(1) + speech(5) + silence(1) + speech(2)


# ---------------------------------------------------------------------------
# Ordered transcription with context
# ---------------------------------------------------------------------------


def test_segments_are_transcribed_in_order_with_the_previous_text_as_context(mic, tmp_path):
    client = FakeClient(["One.", "Two.", "Three."])

    outcome, delivered = _record(mic, client, tmp_path, THREE_SENTENCES)

    assert [(r.index, r.text) for r in delivered] == [(0, "One."), (1, "Two."), (2, "Three.")]
    assert [prompt for _, prompt in client.calls] == [None, "One.", "Two."]
    assert outcome.text == "One. Two. Three."
    assert outcome.failures == []
    assert outcome.stop_reason == "max_duration"


def test_every_recorded_byte_reaches_the_server(mic, tmp_path):
    """The mic never closes between segments: nothing said mid-transcription is dropped."""
    audio = speech(5) + silence(1) + speech(31) + silence(3)
    client = FakeClient(["a", "b", "c", "d"])

    _record(mic, client, tmp_path, audio)

    assert b"".join(pcm for pcm, _ in client.calls) == b"".join(audio)


def test_context_is_dropped_after_a_silent_segment(mic, tmp_path):
    """A prompt over near-silent audio is what makes Whisper echo it back."""
    client = FakeClient(["One.", "", "Three."])

    _record(mic, client, tmp_path, THREE_SENTENCES)

    assert [prompt for _, prompt in client.calls] == [None, "One.", None]


def test_context_is_the_end_of_the_previous_text(mic, tmp_path):
    long_text = "word " * 100
    client = FakeClient([long_text, "next"])

    _record(mic, client, tmp_path, speech(5) + silence(1) + speech(1))

    assert client.calls[1][1] == long_text[-session_mod.PROMPT_CHARS :]


# ---------------------------------------------------------------------------
# Failures: retry, then save the audio
# ---------------------------------------------------------------------------


def test_a_failed_request_is_retried(mic, tmp_path):
    client = FakeClient([TranscriptionUnreachable("restarting"), "One."])

    outcome, delivered = _record(mic, client, tmp_path, speech(2))

    assert [(r.text, r.failed) for r in delivered] == [("One.", False)]
    assert len(client.calls) == 2


def test_audio_that_cannot_be_transcribed_is_saved_with_a_marker(mic, tmp_path):
    attempts = 1 + len(session_mod.RETRY_DELAYS_S)
    client = FakeClient([TranscriptionServerError("model broke")] * attempts + ["Two."])

    outcome, delivered = _record(mic, client, tmp_path, speech(5) + silence(1) + speech(2))

    first, second = delivered
    assert first.failed and "model broke" in (first.error or "")
    prefix, suffix = "[untranscribed audio: talkat file ", "]"
    assert first.text.startswith(prefix) and first.text.endswith(suffix)
    saved = Path(first.text[len(prefix) : -len(suffix)])
    with wave.open(str(saved)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.readframes(wav.getnframes()) == client.calls[0][0]
    assert (second.text, second.failed) == ("Two.", False)
    assert outcome.failures == [first]
    assert outcome.text == f"{first.text} Two."


def test_once_a_segment_fails_later_ones_get_a_single_attempt(mic, tmp_path):
    """A dead server must not cost the full retry backoff on every segment."""
    attempts = 1 + len(session_mod.RETRY_DELAYS_S)
    client = FakeClient([TranscriptionUnreachable("down")] * (attempts + 1) + ["Three."])

    outcome, delivered = _record(mic, client, tmp_path, THREE_SENTENCES)

    assert [r.failed for r in delivered] == [True, True, False]
    assert len(client.calls) == attempts + 1 + 1


# ---------------------------------------------------------------------------
# Stopping, aborting, idling
# ---------------------------------------------------------------------------


def test_stop_ends_capture_and_the_tail_is_still_transcribed(mic, tmp_path):
    mic["pace_s"] = 0.002
    stop_event = threading.Event()
    timer = threading.Timer(0.2, stop_event.set)
    timer.start()
    try:
        outcome, delivered = _record(
            mic,
            FakeClient(["the tail"]),
            tmp_path,
            speech(60),
            stop_event=stop_event,
            max_duration=3600,
        )
    finally:
        timer.cancel()

    assert outcome.stop_reason == "stop_requested"
    assert outcome.text == "the tail"


def test_abort_saves_the_audio_it_did_not_transcribe(mic, tmp_path):
    """Everything recorded but not yet transcribed is saved, and no further
    requests go out.

    The abort has to land once all three segments are recorded and the first
    is in flight, so it waits on both: ``entered`` (the transcription thread
    is inside a request) and ``stop_reason`` (capture has read all the
    audio). Setting it from inside the request instead raced capture — the
    stop arrived mid-stream and the remaining audio became one segment
    rather than two.
    """
    mic["chunks"] = THREE_SENTENCES
    stop_event, abort_event = threading.Event(), threading.Event()
    entered, release = threading.Event(), threading.Event()

    def first_request() -> str:
        entered.set()
        release.wait(5)
        return "One."

    client = FakeClient([first_request, "never sent", "never sent"])
    session = DictationSession(
        client,  # type: ignore[arg-type]
        threshold=200.0,
        max_duration=(len(THREE_SENTENCES) + 0.5) * 0.03,
        stop_event=stop_event,
        abort_event=abort_event,
        untranscribed_dir=tmp_path,
    )

    def abort_once_everything_is_recorded() -> None:
        entered.wait(5)
        while session.stop_reason is None:
            time.sleep(0.01)
        stop_event.set()
        abort_event.set()
        release.set()

    threading.Thread(target=abort_once_everything_is_recorded, daemon=True).start()
    delivered: list[SegmentResult] = []
    try:
        outcome = session.run(delivered.append)
    finally:
        release.set()

    assert outcome.aborted
    assert len(client.calls) == 1, "no requests may start after an abort"
    assert [r.failed for r in delivered] == [False, True, True]
    assert [r.text for r in delivered[1:]] == [
        f"[untranscribed audio: talkat file {path}]" for path in sorted(tmp_path.glob("*.wav"))
    ]


def test_abort_does_not_wait_out_a_hung_request(mic, tmp_path):
    """stop_process's SIGKILL follows its SIGTERM by a second: the in-flight
    segment's audio is saved rather than waited for."""
    release = threading.Event()
    client = FakeClient([lambda: "too late" if release.wait(5) else ""])
    stop_event, abort_event = threading.Event(), threading.Event()
    timer = threading.Timer(0.2, lambda: (stop_event.set(), abort_event.set()))
    timer.start()
    started = time.monotonic()
    try:
        outcome, delivered = _record(
            mic, client, tmp_path, speech(2), stop_event=stop_event, abort_event=abort_event
        )
    finally:
        release.set()
        timer.cancel()

    assert time.monotonic() - started < 2.0
    assert [r.failed for r in delivered] == [True]
    assert len(list(tmp_path.glob("*.wav"))) == 1


def test_idle_timeout_ends_the_session(mic, tmp_path):
    mic["pace_s"] = 0.002
    stop_event = threading.Event()

    outcome, _ = _record(
        mic,
        FakeClient(),
        tmp_path,
        silence(3600),
        stop_event=stop_event,
        max_duration=3600,
        idle_timeout=0.3,
    )

    assert outcome.idle_stopped
    assert stop_event.is_set()


def test_no_idle_reminder_while_a_segment_is_still_being_transcribed(mic, tmp_path):
    """A long sentence in flight is not silence."""
    release = threading.Event()
    reminders: list[float] = []
    client = FakeClient([lambda: "spoken text" if release.wait(3) else ""])
    threading.Timer(0.4, release.set).start()

    _record(
        mic,
        client,
        tmp_path,
        speech(2),
        idle_timeout=3600.0,
        idle_notify_interval=0.05,
        on_idle=reminders.append,
    )

    assert reminders == []


def test_idle_reminders_fire_while_quiet_then_the_session_stops(mic, tmp_path):
    """Silence is not a stop: the caller is told it's still recording, repeatedly."""
    mic["pace_s"] = 0.002
    reminders: list[float] = []

    outcome, _ = _record(
        mic,
        FakeClient(),
        tmp_path,
        silence(3600),
        max_duration=3600,
        idle_timeout=1.5,
        idle_notify_interval=0.1,
        on_idle=reminders.append,
    )

    # ~14 are due in that window; two is plenty of margin for a loaded runner.
    assert len(reminders) >= 2, reminders
    # The reported figure is how long the audio has been quiet, which in this
    # sped-up fake stream runs ahead of wall-clock time.
    assert all(seconds >= 0.1 for seconds in reminders), reminders
    assert outcome.idle_stopped


# ---------------------------------------------------------------------------
# Microphone and callbacks
# ---------------------------------------------------------------------------


def test_a_microphone_that_will_not_open_is_reported(mic, tmp_path):
    mic["available"] = False

    outcome, delivered = _record(mic, FakeClient(), tmp_path, speech(1))

    assert outcome.audio_error is not None
    assert delivered == []


def test_recording_callbacks_fire_once_each(mic, tmp_path):
    started: list[bool] = []
    stopped: list[str | None] = []

    _record(
        mic,
        FakeClient(["hi"]),
        tmp_path,
        speech(1),
        on_recording_started=lambda: started.append(True),
        on_recording_stopped=stopped.append,
    )

    assert started == [True]
    assert stopped == ["max_duration"]
