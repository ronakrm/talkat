"""Integration tests: TranscriptionClient against a real Flask + waitress server on a UDS.

Companion to tests/test_integration_file_processor.py — that one covers
file_processor's /transcribe_file path; this one covers client.TranscriptionClient,
which talks to /transcribe_stream and carries every live dictation segment.

We don't load a real ASR model — the fake server returns canned JSON. What we
DO exercise end-to-end:

  - httpx UDS transport, /transcribe_stream URL routing
  - The wire format: one JSON metadata line, then raw PCM
  - Real JSON response parsing
  - The full TranscriptionUnreachable / TranscriptionServerError mapping for
    ConnectError / HTTPError / JSONDecodeError
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from flask import Flask, jsonify, request
from waitress.server import create_server

from talkat.client import TranscriptionClient, TranscriptionServerError, TranscriptionUnreachable

PCM = np.arange(-800, 800, dtype=np.int16).tobytes()  # 0.1 s of distinguishable samples


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
def recording_server(tmp_path: Path) -> Iterator[tuple[str, list[dict]]]:
    """Returns canned text and records each request's metadata + audio."""
    requests: list[dict] = []
    app = Flask(__name__)

    @app.route("/transcribe_stream", methods=["POST"])
    def transcribe() -> object:
        metadata = json.loads(request.stream.readline())
        requests.append({"metadata": metadata, "audio": request.stream.read()})
        return jsonify({"text": "  hello from canned stream  ", "audio_duration": 0.1})

    for socket_path in _serve(app, tmp_path / "stream.sock"):
        yield socket_path, requests


def _server_returning(tmp_path: Path, body: object, status: int = 200) -> Iterator[str]:
    app = Flask(__name__)

    @app.route("/transcribe_stream", methods=["POST"])
    def transcribe() -> object:
        request.stream.read()
        if isinstance(body, str):
            return body, status, {"Content-Type": "application/json"}
        return jsonify(body), status

    yield from _serve(app, tmp_path / "canned.sock")


def _client(socket_path: str, **config: object) -> TranscriptionClient:
    return TranscriptionClient({"server_socket": socket_path, "http_timeout": 5, **config})


def test_returns_stripped_text_and_sends_metadata_then_pcm(recording_server):
    socket_path, requests = recording_server

    with _client(socket_path) as client:
        text = client.transcribe_audio(PCM)

    assert text == "hello from canned stream"
    assert requests == [{"metadata": {"rate": 16000}, "audio": PCM}]
    assert client.last_metadata["audio_duration"] == pytest.approx(0.1)


def test_prompt_travels_in_the_metadata(recording_server):
    socket_path, requests = recording_server

    with _client(socket_path, language="fr") as client:
        client.transcribe_audio(PCM, prompt="the previous sentence.")
        client.transcribe_audio(PCM, prompt="")

    assert [r["metadata"] for r in requests] == [
        {"rate": 16000, "language": "fr", "prompt": "the previous sentence."},
        {"rate": 16000, "language": "fr"},
    ]


def test_client_can_be_reused(recording_server):
    socket_path, _ = recording_server

    with _client(socket_path) as client:
        first = client.transcribe_audio(PCM)
        second = client.transcribe_audio(PCM)

    assert first == second == "hello from canned stream"


def test_empty_text_comes_back_as_empty_string(tmp_path: Path):
    for socket_path in _server_returning(tmp_path, {"text": ""}):
        with _client(socket_path) as client:
            assert client.transcribe_audio(PCM) == ""


def test_500_raises_server_error(tmp_path: Path):
    for socket_path in _server_returning(tmp_path, {"error": "model broke"}, status=500):
        with _client(socket_path) as client, pytest.raises(TranscriptionServerError):
            client.transcribe_audio(PCM)


def test_malformed_json_raises_server_error(tmp_path: Path):
    for socket_path in _server_returning(tmp_path, "this is not json"):
        with _client(socket_path) as client, pytest.raises(TranscriptionServerError):
            client.transcribe_audio(PCM)


def test_unreachable_socket_raises_unreachable(tmp_path: Path):
    with _client(str(tmp_path / "definitely-does-not-exist.sock")) as client:
        with pytest.raises(TranscriptionUnreachable):
            client.transcribe_audio(PCM)
