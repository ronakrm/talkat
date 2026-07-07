"""Integration tests: TranscriptionClient against a real Flask + waitress server on a UDS.

Companion to tests/test_integration_file_processor.py — that one covers
file_processor's /transcribe_file path; this one covers main.TranscriptionClient
which talks to /transcribe_stream and is the production live-mic transcription path.

We don't load a real ASR model — the fake server returns canned JSON. What we
DO exercise end-to-end:

  - httpx UDS transport with streamed multipart body
  - /transcribe_stream URL routing
  - Real JSON response parsing
  - The full TranscriptionUnreachable / TranscriptionServerError mapping for
    ConnectError / TimeoutException / HTTPError / JSONDecodeError

AudioSession is stubbed (same FakePyAudio pattern as test_vad.py) so we don't
need a real microphone.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from flask import Flask, jsonify, request
from waitress.server import create_server

# ---------------------------------------------------------------------------
# Audio stubs (same pattern as test_vad.py, kept self-contained)
# ---------------------------------------------------------------------------


SAMPLES_PER_CHUNK = 480  # 30 ms at 16 kHz


def _silent_chunk() -> bytes:
    return np.zeros(SAMPLES_PER_CHUNK, dtype=np.int16).tobytes()


def _loud_chunk() -> bytes:
    return np.full(SAMPLES_PER_CHUNK, 5000, dtype=np.int16).tobytes()


class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.queue = list(chunks)

    def read(self, n_samples: int, exception_on_overflow: bool = False) -> bytes:
        if self.queue:
            return self.queue.pop(0)
        return _silent_chunk()

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakePyAudio:
    def __init__(self) -> None:
        pass

    def open(self, **_kwargs: object) -> _FakeStream:
        chunks: list[bytes] = getattr(type(self), "_next_chunks", [])
        return _FakeStream(chunks)

    def terminate(self) -> None:
        pass


@pytest.fixture
def patched_audio(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakePyAudio]]:
    """Stub PyAudio + find_microphone so AudioSession opens without a real mic."""
    from talkat import record as record_mod

    monkeypatch.setattr(record_mod.pyaudio, "PyAudio", _FakePyAudio)
    monkeypatch.setattr(record_mod, "find_microphone", lambda p, preferred_name=None: 0)
    # Feed a short loud burst then silence — enough to satisfy VAD and end.
    _FakePyAudio._next_chunks = [_loud_chunk()] * 3 + [_silent_chunk()] * 30  # type: ignore[attr-defined]
    yield _FakePyAudio


# ---------------------------------------------------------------------------
# Fake-server fixtures
# ---------------------------------------------------------------------------


def _wait_for_socket(socket_path: Path, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"Server did not bind {socket_path} within {timeout}s")


def _serve(app: Flask, socket_path: Path) -> Iterator[str]:
    server = create_server(app, unix_socket=str(socket_path), unix_socket_perms="0600")
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_socket(socket_path)
        yield str(socket_path)
    finally:
        server.close()
        thread.join(timeout=2)


@pytest.fixture
def stream_canned_server(tmp_path: Path) -> Iterator[str]:
    """A fake server that returns canned JSON for /transcribe_stream."""
    app = Flask(__name__)

    @app.route("/transcribe_stream", methods=["POST"])
    def transcribe() -> object:
        # Drain the streamed body so the client's send completes cleanly.
        request.stream.read()
        return jsonify({"text": "  hello from canned stream  "})

    yield from _serve(app, tmp_path / "stream.sock")


@pytest.fixture
def stream_empty_server(tmp_path: Path) -> Iterator[str]:
    """A fake server that returns an empty text field."""
    app = Flask(__name__)

    @app.route("/transcribe_stream", methods=["POST"])
    def transcribe() -> object:
        request.stream.read()
        return jsonify({"text": ""})

    yield from _serve(app, tmp_path / "empty.sock")


@pytest.fixture
def stream_500_server(tmp_path: Path) -> Iterator[str]:
    """A fake server that returns 500 for /transcribe_stream."""
    app = Flask(__name__)

    @app.route("/transcribe_stream", methods=["POST"])
    def transcribe() -> tuple[object, int]:
        request.stream.read()
        return jsonify({"error": "model broke"}), 500

    yield from _serve(app, tmp_path / "five00.sock")


@pytest.fixture
def stream_malformed_json_server(tmp_path: Path) -> Iterator[str]:
    """A fake server that returns 200 but with a non-JSON body."""
    app = Flask(__name__)

    @app.route("/transcribe_stream", methods=["POST"])
    def transcribe() -> tuple[str, int, dict]:
        request.stream.read()
        return "this is not json", 200, {"Content-Type": "application/json"}

    yield from _serve(app, tmp_path / "bad.sock")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(socket_path: str):
    """Build a TranscriptionClient pointed at the given UDS, with short timeout."""
    from talkat.main import TranscriptionClient

    config = {
        "server_socket": socket_path,
        "http_timeout": 5,
        "silence_threshold": 200.0,
        "silence_duration": 0.2,
    }
    return TranscriptionClient(config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_transcription_e2e_returns_canned_text(patched_audio, stream_canned_server: str):
    """The canned text is returned, stripped of leading/trailing whitespace."""
    with _make_client(stream_canned_server) as client:
        text = client.transcribe_one_utterance(max_duration=0.5)

    assert text == "hello from canned stream"


def test_transcription_returns_empty_string_when_server_says_empty(
    patched_audio, stream_empty_server: str
):
    """An empty text field comes back as an empty string, not raising."""
    with _make_client(stream_empty_server) as client:
        text = client.transcribe_one_utterance(max_duration=0.5)

    assert text == ""


def test_transcription_500_raises_server_error(patched_audio, stream_500_server: str):
    """A 500 response must surface as TranscriptionServerError, not a bare HTTPStatusError."""
    from talkat.main import TranscriptionServerError

    with _make_client(stream_500_server) as client:
        with pytest.raises(TranscriptionServerError):
            client.transcribe_one_utterance(max_duration=0.5)


def test_transcription_malformed_json_raises_server_error(
    patched_audio, stream_malformed_json_server: str
):
    """A 200 with non-JSON body must surface as TranscriptionServerError."""
    from talkat.main import TranscriptionServerError

    with _make_client(stream_malformed_json_server) as client:
        with pytest.raises(TranscriptionServerError):
            client.transcribe_one_utterance(max_duration=0.5)


def test_transcription_unreachable_socket_raises_unreachable(patched_audio, tmp_path: Path):
    """A non-existent socket path must surface as TranscriptionUnreachable."""
    from talkat.main import TranscriptionUnreachable

    missing = str(tmp_path / "definitely-does-not-exist.sock")
    with _make_client(missing) as client:
        with pytest.raises(TranscriptionUnreachable):
            client.transcribe_one_utterance(max_duration=0.5)


def test_transcription_client_can_be_reused_after_one_call(
    patched_audio, stream_canned_server: str
):
    """The httpx client doesn't get closed mid-flight; a second call succeeds too."""
    with _make_client(stream_canned_server) as client:
        first = client.transcribe_one_utterance(max_duration=0.5)
        # Re-prime chunks for the second call.
        _FakePyAudio._next_chunks = [_loud_chunk()] * 3 + [_silent_chunk()] * 30  # type: ignore[attr-defined]
        second = client.transcribe_one_utterance(max_duration=0.5)

    assert first == second == "hello from canned stream"
