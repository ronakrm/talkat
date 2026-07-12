"""Toggle-stop signal semantics — the first signal finishes, the second aborts.

``talkat listen`` run a second time delivers SIGINT to the recording
process. That first signal must end the capture loop gracefully so the
in-flight ``/transcribe_stream`` request completes and the transcript is
delivered. v1.0.0's handler raised ``KeyboardInterrupt`` immediately, which
tore down the POST mid-stream and lost every toggle-stopped recording on
real hardware — unseen by the suite because the stop_event tests never
delivered a real signal.

These tests send *real* signals (``os.kill`` to our own PID) while the real
``TranscriptionClient`` streams to a real waitress UDS server; only the
audio hardware is faked. The fake stream paces reads so the main thread is
demonstrably mid-capture when the signal lands.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest
from flask import Flask, jsonify, request
from waitress.server import create_server

# ---------------------------------------------------------------------------
# Handler unit tests — first signal sets the event, second raises
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_signal_handlers() -> Iterator[None]:
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def test_first_signal_sets_event_without_raising(restore_signal_handlers: None):
    from talkat.main import _set_stop_event_on_signal

    stop_event = threading.Event()
    _set_stop_event_on_signal(stop_event)
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)

    handler(signal.SIGINT, None)  # must NOT raise — graceful stop
    assert stop_event.is_set()


def test_second_signal_raises_keyboard_interrupt(restore_signal_handlers: None):
    from talkat.main import _set_stop_event_on_signal

    stop_event = threading.Event()
    _set_stop_event_on_signal(stop_event)
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)

    handler(signal.SIGINT, None)
    with pytest.raises(KeyboardInterrupt):
        handler(signal.SIGINT, None)


def test_sigterm_after_sigint_escalates(restore_signal_handlers: None):
    """stop_process escalates SIGINT → SIGTERM; the SIGTERM is signal #2 and
    must force the abort rather than being absorbed."""
    from talkat.main import _set_stop_event_on_signal

    stop_event = threading.Event()
    _set_stop_event_on_signal(stop_event)
    sigint = signal.getsignal(signal.SIGINT)
    sigterm = signal.getsignal(signal.SIGTERM)
    assert callable(sigint) and callable(sigterm)

    sigint(signal.SIGINT, None)
    with pytest.raises(KeyboardInterrupt):
        sigterm(signal.SIGTERM, None)


# ---------------------------------------------------------------------------
# End-to-end: real SIGINT mid-stream against a real UDS server
# ---------------------------------------------------------------------------

SAMPLES_PER_READ = 480


class _PacedSilentStream:
    """Endless silence, ~3 ms per read: keeps capture running until a signal
    stops it, and gives the interpreter constant bytecode boundaries so a
    pending signal handler runs promptly."""

    def read(self, n_samples: int, exception_on_overflow: bool = False) -> bytes:
        time.sleep(0.003)
        return np.zeros(n_samples, dtype=np.int16).tobytes()

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakePyAudio:
    def open(self, **_kwargs: object) -> _PacedSilentStream:
        return _PacedSilentStream()

    def terminate(self) -> None:
        pass


@pytest.fixture
def patched_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    from talkat import record as record_mod

    monkeypatch.setattr(record_mod.pyaudio, "PyAudio", _FakePyAudio)
    monkeypatch.setattr(record_mod, "find_microphone", lambda p, preferred_name=None: 0)


def _wait_for_socket(socket_path: Path, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        time.sleep(0.02)
    raise TimeoutError(f"Server did not bind {socket_path} within {timeout}s")


def _serve(
    tmp_path: Path, app: Flask, on_teardown: Callable[[], None] | None = None
) -> Iterator[str]:
    socket_path = tmp_path / "toggle.sock"
    server = create_server(app, unix_socket=str(socket_path), unix_socket_perms="0600")
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        _wait_for_socket(socket_path)
        yield str(socket_path)
    finally:
        if on_teardown is not None:
            on_teardown()
        server.close()
        thread.join(timeout=2)


@pytest.fixture
def instant_server(tmp_path: Path) -> Iterator[str]:
    app = Flask(__name__)

    @app.route("/transcribe_stream", methods=["POST"])
    def transcribe() -> object:
        request.stream.read()
        return jsonify({"text": "toggled text"})

    yield from _serve(tmp_path, app)


@pytest.fixture
def slow_server(tmp_path: Path) -> Iterator[str]:
    """Consumes the stream, then stalls — stands in for a hung/slow server.

    The stall is an Event wait so teardown can release the handler thread
    before closing the server (closing under a live handler makes waitress
    traceback on its own trigger fd)."""
    release = threading.Event()
    app = Flask(__name__)

    @app.route("/transcribe_stream", methods=["POST"])
    def transcribe() -> object:
        request.stream.read()
        release.wait(timeout=3.0)
        return jsonify({"text": "too late"})

    def unstall() -> None:
        release.set()
        time.sleep(0.1)

    yield from _serve(tmp_path, app, on_teardown=unstall)


def _client_config(socket_path: str) -> dict[str, object]:
    return {
        "server_socket": socket_path,
        "http_timeout": 10,
        "silence_threshold": 200.0,
        # Far above anything the test waits for: only the signal can stop capture.
        "silence_duration": 60.0,
    }


def test_first_sigint_completes_stream_and_returns_text(
    restore_signal_handlers: None, patched_audio: None, instant_server: str
):
    """THE toggle regression test: SIGINT mid-capture must not lose the
    transcript. The capture loop ends via stop_event, the POST completes,
    and the server's text comes back."""
    from talkat.main import TranscriptionClient, _set_stop_event_on_signal

    stop_event = threading.Event()
    _set_stop_event_on_signal(stop_event)

    timer = threading.Timer(0.35, os.kill, args=(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with TranscriptionClient(_client_config(instant_server)) as client:
            text = client.transcribe_one_utterance(stop_event=stop_event, max_duration=30.0)
    finally:
        timer.cancel()

    assert stop_event.is_set(), "signal was never delivered — test is broken"
    assert text == "toggled text"


def test_second_sigint_aborts_wait_on_slow_server(
    restore_signal_handlers: None, patched_audio: None, slow_server: str
):
    """After the graceful stop, a second signal is the escape hatch: it must
    interrupt the blocking response wait instead of being swallowed by
    PEP 475's auto-retry until http_timeout."""
    from talkat.main import TranscriptionClient, _set_stop_event_on_signal

    stop_event = threading.Event()
    _set_stop_event_on_signal(stop_event)

    first = threading.Timer(0.35, os.kill, args=(os.getpid(), signal.SIGINT))
    second = threading.Timer(1.2, os.kill, args=(os.getpid(), signal.SIGINT))
    first.start()
    second.start()
    started = time.monotonic()
    try:
        with TranscriptionClient(_client_config(slow_server)) as client:
            with pytest.raises(KeyboardInterrupt):
                client.transcribe_one_utterance(stop_event=stop_event, max_duration=30.0)
    finally:
        first.cancel()
        second.cancel()

    elapsed = time.monotonic() - started
    assert stop_event.is_set()
    assert elapsed < 2.5, f"abort took {elapsed:.1f}s — second signal didn't interrupt the wait"
