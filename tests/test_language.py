"""Tests for the §5b language config — validator, plumbing, CLI flag, integration.

§5b makes ``language`` a first-class config knob, with a CLI flag on
``listen``/``long``/``file``/``batch`` and per-request override in the
stream metadata + file-upload form data. Coverage lives in one file so
the language behavior is easy to find.

Integration uses the same fake-server pattern as
``test_integration_transcription_client.py`` — we don't load a real ASR
model, we just verify the metadata line carries ``language`` through.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from flask import Flask, jsonify, request
from waitress.server import create_server

# ---------------------------------------------------------------------------
# validate_language unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["en", "es", "fr", "de", "zh", "ja", "ko", "yue", "auto"])
def test_validate_language_accepts_known_codes(code: str):
    from talkat.security import validate_language

    assert validate_language(code) == code


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "EN",
        "english",
        "en-US",  # BCP-47 with region — not what faster-whisper consumes
        "e",
        "abcd",  # too long for ISO-639
        " en ",  # whitespace
        "en;DROP TABLE",
    ],
)
def test_validate_language_rejects_malformed(bad: str):
    from talkat.security import validate_language

    with pytest.raises(ValueError):
        validate_language(bad)


def test_validate_language_rejects_non_string():
    from talkat.security import validate_language

    with pytest.raises(ValueError, match="must be a string"):
        validate_language(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# CODE_DEFAULTS / validate_json_config
# ---------------------------------------------------------------------------


def test_code_defaults_includes_language_en():
    from talkat.config import CODE_DEFAULTS

    assert CODE_DEFAULTS["language"] == "en"


def test_validate_json_config_accepts_known_language():
    from talkat.security import validate_json_config

    out = validate_json_config({"language": "es"})
    assert out["language"] == "es"


def test_validate_json_config_rejects_bad_language():
    from talkat.security import validate_json_config

    with pytest.raises(ValueError, match="language"):
        validate_json_config({"language": "english"})


# ---------------------------------------------------------------------------
# TranscriptionClient — sends language in metadata when configured
# ---------------------------------------------------------------------------


def test_transcription_client_reads_language_from_config():
    from talkat.main import TranscriptionClient

    client = TranscriptionClient({"server_socket": "/tmp/talkat-test.sock", "language": "fr"})
    try:
        assert client.language == "fr"
    finally:
        client.close()


def test_transcription_client_language_is_none_when_unset():
    """An empty config (no language key) means the client sends no language;
    server applies its own default. This preserves wire-compat with older clients."""
    from talkat.main import TranscriptionClient

    client = TranscriptionClient({"server_socket": "/tmp/talkat-test.sock"})
    try:
        assert client.language is None
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Server-side _resolve_language helper
# ---------------------------------------------------------------------------


def test_resolve_language_returns_default_for_none():
    from talkat import model_server

    model_server._service.default_language = "en"
    assert model_server._resolve_language(None) == "en"


def test_resolve_language_returns_default_for_empty_string():
    from talkat import model_server

    model_server._service.default_language = "ja"
    assert model_server._resolve_language("") == "ja"


def test_resolve_language_passes_through_validated_value():
    from talkat import model_server

    model_server._service.default_language = "en"
    assert model_server._resolve_language("es") == "es"
    assert model_server._resolve_language("auto") == "auto"


def test_resolve_language_raises_on_garbage():
    from talkat import model_server

    with pytest.raises(ValueError):
        model_server._resolve_language("english")


def test_resolve_language_raises_on_non_string():
    from talkat import model_server

    with pytest.raises(ValueError, match="must be a string"):
        model_server._resolve_language(42)


# ---------------------------------------------------------------------------
# CLI flag — present on listen / long / file / batch + plumbed via overrides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subcommand", ["listen", "long", "file", "batch"])
def test_language_flag_present_on_subcommand(subcommand: str):
    """Just running ``--help`` would be the cleanest check but invokes sys.exit.
    We exercise the argparse object built inside cli.main by stubbing sys.argv
    and inspecting captured args via a passthrough."""

    # The argparse setup lives inline in main(); rebuilding it here would
    # duplicate the parser. Instead, monkeypatch sys.argv to ``--language``
    # and confirm we don't get a SystemExit(2) (argparse's "unknown arg" exit).
    from talkat import cli as cli_mod

    extra: list[str] = []
    if subcommand == "file":
        extra = ["dummy.wav"]
    elif subcommand == "batch":
        extra = ["dummy.wav"]
    argv = ["talkat", subcommand, "--language", "es", *extra]

    # We don't want the actual dispatch to run — stub every subcommand sink
    # so main() returns cleanly regardless of which one argparse picks.
    def fake_listen_once(**_kw: object) -> int:
        return 0

    def fake_listen_continuous(**_kw: object) -> int:
        return 0

    def fake_file(*_a: object, **_kw: object) -> int:
        return 0

    def fake_batch(*_a: object, **_kw: object) -> int:
        return 0

    # Stub every dispatch sink so main() returns cleanly regardless of subcommand.
    import talkat.main as main_mod

    monkeypatch_attrs = [
        (main_mod, "listen_once", fake_listen_once),
        (main_mod, "listen_continuous", fake_listen_continuous),
        (cli_mod, "process_audio_file_command", fake_file),
        (cli_mod, "batch_process_files", fake_batch),
    ]

    # Stash & restore — pytest's monkeypatch fixture isn't available in a
    # parametrized helper without re-plumbing. Manual save/restore is enough.
    originals = [(obj, name, getattr(obj, name)) for obj, name, _ in monkeypatch_attrs]
    for obj, name, val in monkeypatch_attrs:
        setattr(obj, name, val)

    # listen needs the listen-lock; bypass it by stubbing ProcessManager.
    class _FakePM:
        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def locked(self, **_kw: object) -> object:
            class _Ctx:
                def __enter__(self_inner) -> object:
                    return self_inner

                def __exit__(self_inner, *_a: object) -> None:
                    pass

            return _Ctx()

        def is_running(self) -> tuple[bool, int | None]:
            return False, None

        def write_pid(self, _pid: int) -> None:
            pass

        def stop_process(self) -> bool:
            return True

    pm_original = cli_mod.ProcessManager
    cli_mod.ProcessManager = _FakePM  # type: ignore[misc, assignment]

    sys_argv_original = sys.argv
    sys.argv = argv

    try:
        try:
            cli_mod.main()
        except SystemExit as e:
            assert e.code == 0, f"main() exited non-zero: {e.code}"
    finally:
        sys.argv = sys_argv_original
        cli_mod.ProcessManager = pm_original  # type: ignore[misc, assignment]
        for obj, name, val in originals:
            setattr(obj, name, val)


def test_overrides_from_args_includes_language():
    from talkat.cli import _overrides_from_args

    args = argparse.Namespace(
        max_recording=None,
        silence_duration=None,
        http_timeout=None,
        language="es",
    )
    overrides = _overrides_from_args(args)
    assert overrides == {"language": "es"}


def test_overrides_from_args_omits_unset_language():
    from talkat.cli import _overrides_from_args

    args = argparse.Namespace(
        max_recording=None,
        silence_duration=None,
        http_timeout=None,
        language=None,
    )
    overrides = _overrides_from_args(args)
    assert overrides == {}


# ---------------------------------------------------------------------------
# Integration: language travels through stream metadata to a fake server
# ---------------------------------------------------------------------------


SAMPLES_PER_CHUNK = 480


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
    _next_chunks: list[bytes] = []

    def open(self, **_kwargs: object) -> _FakeStream:
        return _FakeStream(type(self)._next_chunks)

    def terminate(self) -> None:
        pass


@pytest.fixture
def patched_audio(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakePyAudio]]:
    from talkat import record as record_mod

    monkeypatch.setattr(record_mod.pyaudio, "PyAudio", _FakePyAudio)
    monkeypatch.setattr(record_mod, "find_microphone", lambda: 0)
    _FakePyAudio._next_chunks = [_loud_chunk()] * 3 + [_silent_chunk()] * 30
    yield _FakePyAudio


def _wait_for_socket(socket_path: Path, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"Server did not bind {socket_path} within {timeout}s")


@pytest.fixture
def language_capturing_server(tmp_path: Path) -> Iterator[tuple[str, dict[str, object]]]:
    """Fake /transcribe_stream that records the metadata line it received.

    The body is read line-by-line: metadata is always the first line per the
    protocol; we parse it and stash for assertion.
    """
    captured: dict[str, object] = {}
    app = Flask(__name__)

    @app.route("/transcribe_stream", methods=["POST"])
    def transcribe() -> object:
        metadata_line = request.stream.readline()
        captured["metadata"] = json.loads(metadata_line.decode("utf-8").strip())
        request.stream.read()
        return jsonify({"text": "ok"})

    socket_path = tmp_path / "lang.sock"
    server = create_server(app, unix_socket=str(socket_path), unix_socket_perms="0600")
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_socket(socket_path)
        yield str(socket_path), captured
    finally:
        server.close()
        thread.join(timeout=2)


def test_client_sends_language_in_stream_metadata(
    patched_audio, language_capturing_server: tuple[str, dict[str, object]]
):
    """When the client config has ``language``, it appears in the JSON metadata line."""
    from talkat.main import TranscriptionClient

    socket_path, captured = language_capturing_server
    config = {
        "server_socket": socket_path,
        "http_timeout": 5,
        "silence_threshold": 200.0,
        "silence_duration": 0.2,
        "language": "fr",
    }
    with TranscriptionClient(config) as client:
        client.transcribe_one_utterance(max_duration=0.5)

    assert captured["metadata"] == {"rate": 16000, "language": "fr"}


def test_client_omits_language_when_unset(
    patched_audio, language_capturing_server: tuple[str, dict[str, object]]
):
    """No ``language`` key in config → no ``language`` field in metadata.

    Wire-compat for older clients (and a clean way to defer to the server's
    own default without sending an explicit value).
    """
    from talkat.main import TranscriptionClient

    socket_path, captured = language_capturing_server
    config = {
        "server_socket": socket_path,
        "http_timeout": 5,
        "silence_threshold": 200.0,
        "silence_duration": 0.2,
    }
    with TranscriptionClient(config) as client:
        client.transcribe_one_utterance(max_duration=0.5)

    assert captured["metadata"] == {"rate": 16000}
