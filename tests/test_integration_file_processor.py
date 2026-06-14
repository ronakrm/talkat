"""Integration tests: file_processor against a real Flask + waitress server on a UDS.

We don't load a real Whisper / Vosk model — the fake server returns canned JSON
for /transcribe_file. What we DO exercise end-to-end:

  - httpx unix-domain-socket transport
  - URL routing / health-check path used by file_processor
  - multipart file upload
  - 413 response handling (the §3 max-upload-size feature)
  - client-side size check before the upload
"""

from __future__ import annotations

import threading
import time
import wave
from collections.abc import Iterator
from pathlib import Path

import pytest
from flask import Flask, jsonify, request
from waitress.server import create_server

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pcm_wav(path: Path, duration_seconds: float = 0.25) -> None:
    """Write a tiny mono 16 kHz PCM .wav file at ``path``.

    Content is zero-padded silence — we only care that librosa can decode it.
    """
    sample_rate = 16000
    n_samples = int(sample_rate * duration_seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * n_samples)


def _wait_for_socket(socket_path: Path, timeout: float = 2.0) -> None:
    """Block until ``socket_path`` exists (server bound) or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"Server did not bind {socket_path} within {timeout}s")


# ---------------------------------------------------------------------------
# Fake-server fixtures
# ---------------------------------------------------------------------------


def _serve_app(app: Flask, socket_path: Path) -> Iterator[str]:
    """Start ``app`` on a UDS in a background thread; yield the socket path string."""
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
def canned_response_server(tmp_path: Path) -> Iterator[str]:
    """A fake model server that returns canned transcription JSON for any file."""
    app = Flask(__name__)

    @app.route("/health")
    def health() -> object:
        return jsonify({"status": "ok"})

    @app.route("/transcribe_file", methods=["POST"])
    def transcribe_file() -> object:
        audio = request.files.get("audio")
        # Echo the filename so the test can prove the file actually landed on
        # the server side of the socket.
        name = audio.filename if audio else "<missing>"
        return jsonify({"text": f"canned transcript for {name}", "duration": 0.25})

    yield from _serve_app(app, tmp_path / "canned.sock")


@pytest.fixture
def four_thirteen_server(tmp_path: Path) -> Iterator[str]:
    """A fake server that returns 413 with a JSON error body, matching model_server.py."""
    app = Flask(__name__)

    @app.route("/health")
    def health() -> object:
        return jsonify({"status": "ok"})

    @app.route("/transcribe_file", methods=["POST"])
    def transcribe_file() -> tuple[object, int]:
        return jsonify({"error": "Upload exceeds maximum allowed size of 1 MB"}), 413

    yield from _serve_app(app, tmp_path / "four13.sock")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_transcribe_file_e2e_returns_canned_text(canned_response_server: str, tmp_path: Path):
    """A real WAV uploaded via the real httpx UDS transport round-trips correctly."""
    from talkat.file_processor import transcribe_audio_file

    wav = tmp_path / "fixture.wav"
    _make_pcm_wav(wav)

    text, duration = transcribe_audio_file(str(wav), socket_path=canned_response_server)

    assert text == "canned transcript for fixture.wav"
    assert duration == pytest.approx(0.25, abs=0.05)


def test_transcribe_file_413_aborts_with_systemexit(four_thirteen_server: str, tmp_path: Path):
    """A 413 from the server must surface as sys.exit(1), not a silent return.

    Matches the §3 contract: file_processor checks for status_code == 413 and
    logs the server's error message before exiting.
    """
    from talkat.file_processor import transcribe_audio_file

    wav = tmp_path / "fixture.wav"
    _make_pcm_wav(wav)

    with pytest.raises(SystemExit) as exc_info:
        transcribe_audio_file(str(wav), socket_path=four_thirteen_server)
    assert exc_info.value.code == 1


def test_transcribe_file_oversized_caught_client_side(
    canned_response_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Files larger than max_upload_size_mb must be rejected BEFORE uploading.

    This is the §3 client-side guard: we stat the file and raise ValueError
    before sending bytes over the wire, so the user sees a clean error
    instead of a network/413 round-trip.
    """
    # Pretend the limit is tiny so even our 0.25s fixture trips it.
    # file_processor imports load_app_config inside the function — patch the
    # source module so the in-function import sees the override.
    from talkat import config as config_mod
    from talkat.file_processor import transcribe_audio_file

    real_load = config_mod.load_app_config

    def fake_load() -> dict:
        cfg = real_load()
        cfg["max_upload_size_mb"] = 0  # Anything > 0 bytes is rejected.
        return cfg

    monkeypatch.setattr(config_mod, "load_app_config", fake_load)

    wav = tmp_path / "fixture.wav"
    _make_pcm_wav(wav)

    with pytest.raises(ValueError) as exc_info:
        transcribe_audio_file(str(wav), socket_path=canned_response_server)
    assert "exceeds" in str(exc_info.value)


def test_transcribe_file_server_unreachable_aborts(tmp_path: Path):
    """When the socket doesn't exist, transcribe_audio_file must sys.exit(1)."""
    from talkat.file_processor import transcribe_audio_file

    wav = tmp_path / "fixture.wav"
    _make_pcm_wav(wav)
    missing_socket = str(tmp_path / "does_not_exist.sock")

    with pytest.raises(SystemExit) as exc_info:
        transcribe_audio_file(wav.as_posix(), socket_path=missing_socket)
    assert exc_info.value.code == 1


def test_transcribe_file_rejects_unsupported_extension(canned_response_server: str, tmp_path: Path):
    """Files with an unsupported extension never reach the server."""
    from talkat.file_processor import transcribe_audio_file

    bogus = tmp_path / "fixture.xyz"
    bogus.write_bytes(b"not audio")

    with pytest.raises(ValueError, match="Unsupported file format"):
        transcribe_audio_file(str(bogus), socket_path=canned_response_server)
