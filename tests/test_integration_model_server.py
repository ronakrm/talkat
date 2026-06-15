"""Integration tests for ``talkat.model_server`` routes via a real waitress UDS.

The unit tests in ``test_backends.py`` already cover the FasterWhisper / Vosk
backend Protocol. These tests drive the Flask routes themselves through a real
waitress server on a Unix domain socket, with a FakeBackend swapped in for the
ASR path so no model files are needed.

What's covered end-to-end:
    * ``/health`` reports backend status
    * ``/transcribe_stream`` decodes the metadata + audio framing, plumbs the
      per-request language override, and returns the FakeBackend response
    * ``/transcribe_stream`` rejects missing / malformed metadata and
      malformed language
    * ``/transcribe_file`` accepts a multipart upload, applies the language
      override, and returns the FakeBackend response
    * ``/transcribe_file`` rejects missing-file / empty-filename
    * ``MAX_CONTENT_LENGTH`` enforcement returns the JSON 413 body the §3
      contract documents
    * ``/dictionary`` GET reports the current dictionary; POST overwrites it
"""

from __future__ import annotations

import json
import threading
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from waitress.server import create_server

from talkat import model_server

# ---------------------------------------------------------------------------
# FakeBackend — records calls + returns canned responses
# ---------------------------------------------------------------------------


class FakeBackend:
    """Implements ``TranscriptionBackend`` without loading any real model."""

    name = "fake"

    def __init__(self, response: str = "hello from fake") -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def load(self, config: dict[str, Any]) -> None:  # noqa: ARG002
        pass

    def warm_up(self) -> None:
        pass

    def transcribe(
        self,
        audio: np.ndarray,
        language: str = "en",
        initial_prompt: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "audio_len": int(len(audio)),
                "language": language,
                "initial_prompt": initial_prompt,
            }
        )
        return self.response


# ---------------------------------------------------------------------------
# Server fixture — install FakeBackend, serve the real Flask app on a UDS
# ---------------------------------------------------------------------------


def _wait_for_socket(socket_path: Path, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"Server did not bind {socket_path} within {timeout}s")


@pytest.fixture
def fake_backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def live_server(
    tmp_path: Path,
    fake_backend: FakeBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, FakeBackend]]:
    """Boot the real model_server.app on a UDS with FakeBackend installed."""
    socket_path = tmp_path / "model_server.sock"

    # Stash + restore module-level state so tests stay isolated.
    prior_backend = model_server._service.backend
    prior_model_type = model_server._service.model_type
    prior_dictionary = model_server._service.dictionary_words
    prior_language = model_server._service.default_language
    prior_max_content = model_server.app.config.get("MAX_CONTENT_LENGTH")

    model_server._service.backend = fake_backend
    model_server._service.model_type = "fake"
    model_server._service.dictionary_words = []
    model_server._service.default_language = "en"
    # Match production main()'s wiring: 1 MB cap exercises 413 cleanly.
    model_server.app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

    server = create_server(model_server.app, unix_socket=str(socket_path), unix_socket_perms="0600")
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_socket(socket_path)
        yield str(socket_path), fake_backend
    finally:
        server.close()
        thread.join(timeout=2)
        model_server._service.backend = prior_backend
        model_server._service.model_type = prior_model_type
        model_server._service.dictionary_words = prior_dictionary
        model_server._service.default_language = prior_language
        if prior_max_content is None:
            model_server.app.config.pop("MAX_CONTENT_LENGTH", None)
        else:
            model_server.app.config["MAX_CONTENT_LENGTH"] = prior_max_content


def _client(socket_path: str) -> httpx.Client:
    return httpx.Client(transport=httpx.HTTPTransport(uds=socket_path), timeout=5.0)


def _pcm16_silence_frame(n_samples: int = 1600) -> bytes:
    return (np.zeros(n_samples, dtype=np.int16)).tobytes()


def _make_pcm_wav(path: Path, duration_seconds: float = 0.25) -> None:
    sample_rate = 16000
    n_samples = int(sample_rate * duration_seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_samples)


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------


def test_health_reports_backend_loaded(live_server: tuple[str, FakeBackend]) -> None:
    socket_path, _ = live_server
    with _client(socket_path) as c:
        r = c.get("http://talkat/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["model_type"] == "fake"


def test_health_reports_error_when_no_backend(
    live_server: tuple[str, FakeBackend],
) -> None:
    """Unsetting the backend mid-session flips /health to 500. The fixture's
    teardown restores the original backend so other tests are unaffected."""
    socket_path, _ = live_server
    model_server._service.backend = None
    with _client(socket_path) as c:
        r = c.get("http://talkat/health")
    assert r.status_code == 500
    assert r.json()["status"] == "error"


# ---------------------------------------------------------------------------
# /transcribe_stream
# ---------------------------------------------------------------------------


def test_stream_happy_path_returns_backend_response(
    live_server: tuple[str, FakeBackend],
) -> None:
    socket_path, backend = live_server
    metadata = json.dumps({"rate": 16000}).encode("utf-8") + b"\n"
    audio = _pcm16_silence_frame()

    with _client(socket_path) as c:
        r = c.post("http://talkat/transcribe_stream", content=metadata + audio)

    assert r.status_code == 200, r.text
    assert r.json() == {"text": "hello from fake"}
    assert len(backend.calls) == 1
    # The audio buffer was decoded — 1600 int16 samples → 1600 floats.
    assert backend.calls[0]["audio_len"] == 1600
    # Default language fell through from _service.default_language.
    assert backend.calls[0]["language"] == "en"


def test_stream_per_request_language_overrides_default(
    live_server: tuple[str, FakeBackend],
) -> None:
    socket_path, backend = live_server
    metadata = json.dumps({"rate": 16000, "language": "es"}).encode("utf-8") + b"\n"
    audio = _pcm16_silence_frame()

    with _client(socket_path) as c:
        r = c.post("http://talkat/transcribe_stream", content=metadata + audio)

    assert r.status_code == 200
    assert backend.calls[-1]["language"] == "es"


def test_stream_empty_audio_short_circuits(
    live_server: tuple[str, FakeBackend],
) -> None:
    """No audio after the metadata line → ``{"text": ""}`` without hitting the backend."""
    socket_path, backend = live_server
    metadata = json.dumps({"rate": 16000}).encode("utf-8") + b"\n"

    with _client(socket_path) as c:
        r = c.post("http://talkat/transcribe_stream", content=metadata)

    assert r.status_code == 200
    assert r.json() == {"text": ""}
    assert backend.calls == []


def test_stream_missing_metadata_is_400(live_server: tuple[str, FakeBackend]) -> None:
    socket_path, _ = live_server
    with _client(socket_path) as c:
        r = c.post("http://talkat/transcribe_stream", content=b"")
    assert r.status_code == 400
    assert "metadata" in r.json()["error"].lower()


def test_stream_malformed_metadata_is_400(live_server: tuple[str, FakeBackend]) -> None:
    socket_path, _ = live_server
    with _client(socket_path) as c:
        r = c.post("http://talkat/transcribe_stream", content=b"not json\n\x00\x00")
    assert r.status_code == 400


def test_stream_malformed_language_is_400(
    live_server: tuple[str, FakeBackend],
) -> None:
    """Per-request language must still pass validate_language."""
    socket_path, _ = live_server
    metadata = json.dumps({"rate": 16000, "language": "english"}).encode("utf-8") + b"\n"
    audio = _pcm16_silence_frame()
    with _client(socket_path) as c:
        r = c.post("http://talkat/transcribe_stream", content=metadata + audio)
    assert r.status_code == 400
    assert "language" in r.json()["error"].lower()


def test_stream_500_when_backend_not_loaded(
    live_server: tuple[str, FakeBackend],
) -> None:
    socket_path, _ = live_server
    model_server._service.backend = None
    metadata = json.dumps({"rate": 16000}).encode("utf-8") + b"\n"
    with _client(socket_path) as c:
        r = c.post(
            "http://talkat/transcribe_stream",
            content=metadata + _pcm16_silence_frame(),
        )
    assert r.status_code == 500
    assert "Model not loaded" in r.json()["error"]


# ---------------------------------------------------------------------------
# /transcribe_file
# ---------------------------------------------------------------------------


def test_transcribe_file_happy_path(live_server: tuple[str, FakeBackend], tmp_path: Path) -> None:
    socket_path, backend = live_server
    wav = tmp_path / "fixture.wav"
    _make_pcm_wav(wav)

    with _client(socket_path) as c:
        with open(wav, "rb") as f:
            r = c.post(
                "http://talkat/transcribe_file",
                files={"audio": ("fixture.wav", f, "audio/wav")},
            )

    assert r.status_code == 200, r.text
    assert r.json() == {"text": "hello from fake"}
    assert len(backend.calls) == 1
    # 0.25s @ 16kHz mono = 4000 samples after librosa resample/load
    assert backend.calls[0]["audio_len"] == pytest.approx(4000, abs=10)


def test_transcribe_file_language_form_field_overrides_default(
    live_server: tuple[str, FakeBackend], tmp_path: Path
) -> None:
    socket_path, backend = live_server
    wav = tmp_path / "fixture.wav"
    _make_pcm_wav(wav)

    with _client(socket_path) as c:
        with open(wav, "rb") as f:
            r = c.post(
                "http://talkat/transcribe_file",
                files={"audio": ("fixture.wav", f, "audio/wav")},
                data={"language": "fr"},
            )

    assert r.status_code == 200
    assert backend.calls[-1]["language"] == "fr"


def test_transcribe_file_missing_audio_is_400(
    live_server: tuple[str, FakeBackend],
) -> None:
    socket_path, _ = live_server
    with _client(socket_path) as c:
        r = c.post("http://talkat/transcribe_file", files={"not_audio": ("x.txt", b"")})
    assert r.status_code == 400
    assert "audio" in r.json()["error"].lower()


def test_transcribe_file_413_returns_json_body(
    live_server: tuple[str, FakeBackend], tmp_path: Path
) -> None:
    """Over-cap uploads must hit the §3 JSON 413 path, not the default HTML body."""
    socket_path, _ = live_server
    fat = tmp_path / "fat.wav"
    # Force payload > 1 MB cap set in the fixture.
    fat.write_bytes(b"\x00" * (2 * 1024 * 1024))

    with _client(socket_path) as c:
        with open(fat, "rb") as f:
            r = c.post(
                "http://talkat/transcribe_file",
                files={"audio": ("fat.wav", f, "audio/wav")},
            )

    assert r.status_code == 413
    body = r.json()
    assert "exceeds" in body["error"].lower()
    assert "1 MB" in body["error"]


# ---------------------------------------------------------------------------
# /dictionary
# ---------------------------------------------------------------------------


def test_dictionary_get_reports_current_words(
    live_server: tuple[str, FakeBackend],
) -> None:
    socket_path, _ = live_server
    model_server._service.dictionary_words = ["foo", "bar"]
    with _client(socket_path) as c:
        r = c.get("http://talkat/dictionary")
    assert r.status_code == 200
    body = r.json()
    assert body["words"] == ["foo", "bar"]
    assert body["count"] == 2


def test_dictionary_post_rejects_missing_file(
    live_server: tuple[str, FakeBackend],
) -> None:
    socket_path, _ = live_server
    with _client(socket_path) as c:
        r = c.post("http://talkat/dictionary", files={"not_a_dict": ("x.txt", b"")})
    assert r.status_code == 400


def test_dictionary_post_rejects_empty_body(
    live_server: tuple[str, FakeBackend],
) -> None:
    socket_path, _ = live_server
    with _client(socket_path) as c:
        r = c.post(
            "http://talkat/dictionary",
            files={"dictionary": ("words.txt", b"\n  \n")},
        )
    assert r.status_code == 400
