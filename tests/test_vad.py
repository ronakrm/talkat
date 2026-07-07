"""VAD unit tests for talkat.record.AudioSession.

The microphone and PyAudio are stubbed out; we drive the iterator with
synthetic int16 chunks of either silence (volume ~0) or speech (volume well
above the threshold). What we assert:

  - every chunk is yielded from stream-open (the threshold never gates what
    is SENT — that's how utterance beginnings got clipped; it only decides
    when to stop)
  - silence_duration ends iteration after speech
  - pure silence never triggers the silence auto-stop (runs to max_duration)
  - max_duration caps iteration
  - stop_event short-circuits the loop
  - a failed stream-open is retried once with a fresh PyAudio instance
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
    monkeypatch.setattr(record_mod, "find_microphone", lambda p, preferred_name=None: 0)
    yield FakePyAudio


def _drive_session(
    patched_pyaudio: type[FakePyAudio],
    chunks: list[bytes],
    *,
    threshold: float,
    silence_duration: float = 0.5,
    max_duration: float | None = 1.0,
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


def test_pure_silence_streams_until_max_duration(patched_pyaudio):
    """A pure-silence stream is still sent (server VAD handles it) and only
    max_duration ends it — the silence auto-stop needs speech first."""
    chunks = [_silent_chunk() for _ in range(20)]
    yielded = _drive_session(
        patched_pyaudio,
        chunks,
        threshold=200,
        silence_duration=0.2,
        max_duration=0.48,  # 0.48 * 16000 / 480 = 16 chunks exactly
    )
    assert len(yielded) == 16


def test_all_audio_streamed_from_open_including_leading_silence(patched_pyaudio):
    """Audio before the VAD trigger must be yielded too, in order.

    This is the cut-off-beginnings fix: the threshold must never gate what is
    sent. Leading silent chunks (and with them, quiet speech onsets an RMS
    threshold would miss) all reach the server.
    """
    silent = _silent_chunk()
    loud = _loud_chunk()
    chunks = [silent] * 3 + [loud] * 2 + [silent] * 20

    yielded = _drive_session(
        patched_pyaudio,
        chunks,
        threshold=200,
        silence_duration=0.2,
        max_duration=2.0,
    )

    # The full leading-silence prefix and the speech must be present, in order.
    assert yielded[:5] == [silent, silent, silent, loud, loud]


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

    monkeypatch.setattr(record_mod, "find_microphone", lambda p, preferred_name=None: None)

    with pytest.raises(AudioSessionError):
        with AudioSession(
            threshold=0,
            silence_duration=0.5,
            max_duration=0.09,
            chunk_size_ms=30,
        ):
            pass


def test_audiosession_retries_open_with_fresh_instance(patched_pyaudio, monkeypatch):
    """A transient stream-open failure must be retried on a new PyAudio instance.

    This is the [Errno -9998] fix: PipeWire topology churn can invalidate a
    device index between snapshots; one retry with a fresh enumeration
    absorbs it.
    """
    from talkat import record as record_mod
    from talkat.record import AudioSession

    instances: list[object] = []

    class FlakyPyAudio(FakePyAudio):
        def open(self, **kwargs: object) -> FakeStream:
            instances.append(self)
            if len(instances) == 1:
                raise OSError(-9998, "Invalid number of channels")
            return super().open(**kwargs)

    monkeypatch.setattr(record_mod.pyaudio, "PyAudio", FlakyPyAudio)
    monkeypatch.setattr(record_mod.AudioSession, "OPEN_RETRY_DELAY_S", 0.0)
    FlakyPyAudio._next_chunks = [_silent_chunk()] * 3  # type: ignore[attr-defined]

    with AudioSession(
        threshold=0,
        silence_duration=0.5,
        max_duration=0.09,
        chunk_size_ms=30,
    ) as session:
        yielded = list(iter(session))

    assert len(instances) == 2, "expected one retry on a fresh PyAudio instance"
    assert instances[0] is not instances[1]
    assert isinstance(instances[0], FlakyPyAudio) and instances[0].terminated
    assert len(yielded) == 3


def test_audiosession_raises_after_all_open_attempts_fail(patched_pyaudio, monkeypatch):
    """Persistent open failure surfaces as AudioSessionError after retries."""
    from talkat import record as record_mod
    from talkat.record import AudioSession, AudioSessionError

    class AlwaysFailingPyAudio(FakePyAudio):
        def open(self, **kwargs: object) -> FakeStream:
            raise OSError(-9998, "Invalid number of channels")

    monkeypatch.setattr(record_mod.pyaudio, "PyAudio", AlwaysFailingPyAudio)
    monkeypatch.setattr(record_mod.AudioSession, "OPEN_RETRY_DELAY_S", 0.0)

    with pytest.raises(AudioSessionError):
        with AudioSession(threshold=0, silence_duration=0.5, max_duration=0.09):
            pass


# ---------------------------------------------------------------------------
# calibrate_microphone — same PyAudio stub pattern as AudioSession but
# different chunk size (default 1024) and no VAD.
# ---------------------------------------------------------------------------


CALIBRATE_CHUNK_SAMPLES = 1024  # calibrate_microphone's fixed CHUNK


def _calibrate_chunk(amplitude: int) -> bytes:
    """A chunk sized for calibrate_microphone's default CHUNK=1024."""
    return np.full(CALIBRATE_CHUNK_SAMPLES, amplitude, dtype=np.int16).tobytes()


@pytest.fixture
def patched_pyaudio_for_calibrate(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[type[FakePyAudio]]:
    """Same as patched_pyaudio but also silences calibrate's notify-send calls."""
    from talkat import record as record_mod

    monkeypatch.setattr(record_mod.pyaudio, "PyAudio", FakePyAudio)
    monkeypatch.setattr(record_mod, "find_microphone", lambda p, preferred_name=None: 0)
    monkeypatch.setattr(record_mod, "safe_subprocess_run", lambda *_a, **_k: None)
    yield FakePyAudio


def test_calibrate_clamps_pure_silence_to_min_threshold(patched_pyaudio_for_calibrate, capsys):
    """A perfectly silent stream produces p95=0; result is clamped to silence_threshold_min."""
    from talkat.config import CODE_DEFAULTS
    from talkat.record import calibrate_microphone

    patched_pyaudio_for_calibrate._next_chunks = [_calibrate_chunk(0) for _ in range(16)]
    threshold = calibrate_microphone(duration=1)

    assert threshold == CODE_DEFAULTS["silence_threshold_min"]


def test_calibrate_clamps_loud_audio_to_max_threshold(patched_pyaudio_for_calibrate):
    """A uniformly loud stream produces p95=5000; result is clamped to silence_threshold_max."""
    from talkat.config import CODE_DEFAULTS
    from talkat.record import calibrate_microphone

    patched_pyaudio_for_calibrate._next_chunks = [_calibrate_chunk(5000) for _ in range(16)]
    threshold = calibrate_microphone(duration=1)

    assert threshold == CODE_DEFAULTS["silence_threshold_max"]


def test_calibrate_returns_p95_for_typical_noise(patched_pyaudio_for_calibrate):
    """For a noise profile between the min/max bounds, the returned threshold tracks p95."""
    from talkat.record import calibrate_microphone

    # 16 chunks at amplitude 400 → RMS volume = 400; p95 = 400, well within
    # the [50, 5000] clamp window.
    patched_pyaudio_for_calibrate._next_chunks = [_calibrate_chunk(400) for _ in range(16)]
    threshold = calibrate_microphone(duration=1)

    assert threshold == pytest.approx(400.0, abs=1.0)


def test_calibrate_falls_back_when_no_microphone(monkeypatch: pytest.MonkeyPatch):
    """No mic → returns silence_threshold_fallback (and terminates PyAudio)."""
    from talkat import record as record_mod
    from talkat.config import CODE_DEFAULTS
    from talkat.record import calibrate_microphone

    monkeypatch.setattr(record_mod.pyaudio, "PyAudio", FakePyAudio)
    monkeypatch.setattr(record_mod, "find_microphone", lambda p, preferred_name=None: None)
    monkeypatch.setattr(record_mod, "safe_subprocess_run", lambda *_a, **_k: None)

    threshold = calibrate_microphone(duration=1)
    assert threshold == CODE_DEFAULTS["silence_threshold_fallback"]


def test_calibrate_falls_back_when_stream_open_fails(monkeypatch: pytest.MonkeyPatch):
    """If PyAudio.open raises, calibrate returns the fallback threshold rather than crashing."""
    from talkat import record as record_mod
    from talkat.config import CODE_DEFAULTS
    from talkat.record import calibrate_microphone

    class FailingPyAudio:
        def __init__(self) -> None:
            pass

        def open(self, **_kwargs: object) -> None:
            raise OSError("simulated stream-open failure")

        def terminate(self) -> None:
            pass

    monkeypatch.setattr(record_mod.pyaudio, "PyAudio", FailingPyAudio)
    monkeypatch.setattr(record_mod, "find_microphone", lambda p, preferred_name=None: 0)
    monkeypatch.setattr(record_mod, "safe_subprocess_run", lambda *_a, **_k: None)

    threshold = calibrate_microphone(duration=1)
    assert threshold == CODE_DEFAULTS["silence_threshold_fallback"]


def test_calibrate_respects_custom_min_max_from_config(
    patched_pyaudio_for_calibrate, monkeypatch: pytest.MonkeyPatch
):
    """User-configured silence_threshold_min/max must override the defaults."""
    from talkat import record as record_mod
    from talkat.record import calibrate_microphone

    custom_cfg = {
        "audio_chunk_size": 1024,
        "audio_channels": 1,
        "audio_sample_rate": 16000,
        "silence_threshold_min": 100.0,
        "silence_threshold_max": 1000.0,
        "silence_threshold_fallback": 500.0,
    }
    monkeypatch.setattr(record_mod, "load_app_config", lambda: custom_cfg)

    # 16 silent chunks → p95 = 0 → clamped to custom min (100.0)
    patched_pyaudio_for_calibrate._next_chunks = [_calibrate_chunk(0) for _ in range(16)]
    threshold = calibrate_microphone(duration=1)
    assert threshold == 100.0
