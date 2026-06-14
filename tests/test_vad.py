"""VAD unit tests for talkat.record.AudioSession.

The microphone and PyAudio are stubbed out; we drive the iterator with
synthetic int16 chunks of either silence (volume ~0) or speech (volume well
above the threshold). What we assert:

  - no-VAD mode (threshold=0) yields every chunk
  - pure silence in VAD mode yields nothing
  - speech triggers pre-roll inclusion (buffered silent chunks are yielded
    when speech first starts)
  - silence_duration ends iteration after speech
  - max_duration caps iteration
  - stop_event short-circuits the loop
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


SAMPLES_PER_CHUNK = 480  # 30 ms at 16 kHz


def _loud_chunk(amplitude: int = 5000) -> bytes:
    """A chunk whose RMS volume is well above the default threshold (200)."""
    return np.full(SAMPLES_PER_CHUNK, amplitude, dtype=np.int16).tobytes()


def _silent_chunk() -> bytes:
    return np.zeros(SAMPLES_PER_CHUNK, dtype=np.int16).tobytes()


class FakeStream:
    """Stand-in for pyaudio.Stream that returns queued bytes, then silence forever."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.queue: list[bytes] = list(chunks)
        self.reads = 0
        self.closed = False
        self.stopped = False

    def read(self, n_samples: int, exception_on_overflow: bool = False) -> bytes:
        self.reads += 1
        if self.queue:
            return self.queue.pop(0)
        # Endless silence — the iterator should terminate via VAD / max_duration
        # / stop_event before this becomes load-bearing.
        return _silent_chunk()

    def stop_stream(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


class FakePyAudio:
    """Stand-in for pyaudio.PyAudio that returns a single shared FakeStream."""

    def __init__(self) -> None:
        self._stream: FakeStream | None = None
        self.terminated = False

    def open(self, **kwargs: object) -> FakeStream:
        # Tests inject the chunks via a sentinel set on the class beforehand.
        chunks: list[bytes] = getattr(type(self), "_next_chunks", [])
        self._stream = FakeStream(chunks)
        return self._stream

    def terminate(self) -> None:
        self.terminated = True


@pytest.fixture
def patched_pyaudio(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakePyAudio]]:
    """Replace pyaudio.PyAudio and stub find_microphone so AudioSession.__enter__ works."""
    from talkat import record as record_mod

    monkeypatch.setattr(record_mod.pyaudio, "PyAudio", FakePyAudio)
    monkeypatch.setattr(record_mod, "find_microphone", lambda: 0)
    yield FakePyAudio


def _drive_session(
    patched_pyaudio: type[FakePyAudio],
    chunks: list[bytes],
    *,
    threshold: float,
    silence_duration: float = 0.5,
    max_duration: float | None = 1.0,
    pre_speech_padding: float = 0.06,
    stop_event: threading.Event | None = None,
) -> list[bytes]:
    """Open an AudioSession with the given chunks and return everything it yields."""
    from talkat.record import AudioSession

    patched_pyaudio._next_chunks = list(chunks)  # type: ignore[attr-defined]
    yielded: list[bytes] = []
    with AudioSession(
        threshold=threshold,
        silence_duration=silence_duration,
        max_duration=max_duration,
        pre_speech_padding=pre_speech_padding,
        chunk_size_ms=30,
        stop_event=stop_event,
    ) as session:
        for chunk in session:
            yielded.append(chunk)
    return yielded


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_vad_mode_yields_every_chunk(patched_pyaudio):
    """threshold=0 disables VAD; every chunk read must be yielded."""
    # max_duration=0.15s → max_total_chunks = 0.15 * 16000 / 480 = 5 exactly.
    chunks = [_silent_chunk() for _ in range(5)]
    yielded = _drive_session(
        patched_pyaudio,
        chunks,
        threshold=0,
        max_duration=0.15,
    )
    assert len(yielded) == 5


def test_pure_silence_yields_nothing(patched_pyaudio):
    """In VAD mode, a pure-silence stream must not yield any chunks."""
    chunks = [_silent_chunk() for _ in range(20)]
    yielded = _drive_session(
        patched_pyaudio,
        chunks,
        threshold=200,
        silence_duration=0.2,
        max_duration=0.5,  # 0.5 * 16000 / 480 ≈ 16 chunks read
    )
    assert yielded == []


def test_speech_triggers_pre_roll(patched_pyaudio):
    """When speech is detected, the pre-speech buffer must be flushed first.

    With pre_speech_padding=0.06 (2 chunks), the two silent chunks immediately
    preceding the first loud chunk should appear in the yielded sequence — they
    can't have been generated mid-speech (loud chunks differ byte-for-byte from
    the silent ones we feed in).
    """
    silent = _silent_chunk()
    loud = _loud_chunk()
    # 3 silent → only the last 2 survive in the deque(maxlen=2).
    # Then 2 loud → speech triggers; pre-roll + loud chunks yielded.
    # Then 20 silent → enough to trigger silence_duration exit.
    chunks = [silent] * 3 + [loud] * 2 + [silent] * 20

    yielded = _drive_session(
        patched_pyaudio,
        chunks,
        threshold=200,
        silence_duration=0.2,
        max_duration=2.0,
        pre_speech_padding=0.06,
    )

    # First yielded chunk must be silent (pre-roll).
    assert yielded[0] == silent, "pre-roll silent chunk should come first"
    # The loud chunks must show up.
    assert loud in yielded


def test_silence_duration_ends_iteration_after_speech(patched_pyaudio):
    """After speech, ``silence_duration`` seconds of quiet must stop iteration.

    With silence_duration=0.2s and a 30ms chunk, max_silent_chunks = 6. Once
    silent_chunks exceeds 6 the iterator returns — well before the 100-chunk
    feed runs out.
    """
    chunks = [_loud_chunk()] * 3 + [_silent_chunk()] * 100

    yielded = _drive_session(
        patched_pyaudio,
        chunks,
        threshold=200,
        silence_duration=0.2,
        max_duration=5.0,
        pre_speech_padding=0.06,
    )

    # We should NOT have yielded all 100 trailing silent chunks — silence
    # termination must kick in well before.
    assert len(yielded) < 30, f"silence_duration didn't cut off — yielded {len(yielded)}"
    # But we should have yielded the loud speech chunks.
    assert _loud_chunk() in yielded


def test_max_duration_caps_iteration(patched_pyaudio):
    """max_duration must bound the total chunks read regardless of VAD state."""
    # 100 chunks fed; threshold=0 so every read attempts to yield.
    chunks = [_silent_chunk() for _ in range(100)]
    yielded = _drive_session(
        patched_pyaudio,
        chunks,
        threshold=0,
        max_duration=0.09,  # 0.09 * 16000 / 480 = 3 chunks exactly
    )
    assert len(yielded) == 3


def test_stop_event_short_circuits_iteration(patched_pyaudio):
    """A pre-set stop_event must make iteration return before reading anything."""
    stop_event = threading.Event()
    stop_event.set()

    chunks = [_loud_chunk()] * 10  # would otherwise yield freely
    yielded = _drive_session(
        patched_pyaudio,
        chunks,
        threshold=0,  # no-VAD; every chunk would yield
        max_duration=1.0,
        stop_event=stop_event,
    )

    assert yielded == [], "iteration ran despite stop_event being set"


def test_audiosession_closes_resources_on_exit(patched_pyaudio):
    """The context manager must stop and close the stream on exit."""
    from talkat.record import AudioSession

    patched_pyaudio._next_chunks = [_silent_chunk()] * 3  # type: ignore[attr-defined]
    with AudioSession(
        threshold=0,
        silence_duration=0.5,
        max_duration=0.09,
        chunk_size_ms=30,
    ) as session:
        # Consume the iterator so the stream is fully exercised.
        list(iter(session))
        stream = session._stream
        pa = session._p
    assert stream is not None and stream.stopped
    assert stream.closed
    assert pa is not None and pa.terminated
    # Session attributes should be cleared.
    assert session._stream is None
    assert session._p is None


def test_audiosession_raises_when_no_microphone(patched_pyaudio, monkeypatch):
    """If find_microphone returns None, __enter__ raises AudioSessionError."""
    from talkat import record as record_mod
    from talkat.record import AudioSession, AudioSessionError

    monkeypatch.setattr(record_mod, "find_microphone", lambda: None)

    with pytest.raises(AudioSessionError):
        with AudioSession(
            threshold=0,
            silence_duration=0.5,
            max_duration=0.09,
            chunk_size_ms=30,
        ):
            pass
