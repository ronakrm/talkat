"""Toggle-stop signal semantics — the first signal finishes, the second aborts.

``talkat listen`` run a second time delivers SIGINT to the recording
process. That first signal must end capture gracefully so everything
recorded is still transcribed and delivered. v1.0.0's handler raised
``KeyboardInterrupt`` immediately, which tore down the request in flight and
lost every toggle-stopped recording on real hardware — unseen by the suite
because the stop_event tests never delivered a real signal.

A second signal (stop_process escalating to SIGTERM) aborts: no new
requests, untranscribed audio saved, out within stop_process's one-second
SIGKILL window. Neither signal raises — a KeyboardInterrupt inside
``subprocess.run`` SIGKILLs ydotool mid-keystroke, leaving the key held down.

The end-to-end tests send *real* signals (``os.kill`` to our own PID) while a
real DictationSession streams to a real waitress UDS server; only the audio
hardware is faked.
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
# Handler unit tests
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_signal_handlers() -> Iterator[None]:
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def _installed() -> tuple[threading.Event, threading.Event]:
    from talkat.main import _set_stop_event_on_signal

    stop_event, abort_event = threading.Event(), threading.Event()
    _set_stop_event_on_signal(stop_event, abort_event)
    return stop_event, abort_event


def test_first_signal_stops_without_aborting(restore_signal_handlers: None):
    stop_event, abort_event = _installed()
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)

    handler(signal.SIGINT, None)  # must NOT raise — graceful stop
    assert stop_event.is_set()
    assert not abort_event.is_set()


def test_second_signal_aborts_without_raising(restore_signal_handlers: None):
    stop_event, abort_event = _installed()
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)

    handler(signal.SIGINT, None)
    handler(signal.SIGINT, None)  # must NOT raise either
    assert abort_event.is_set()


def test_sigterm_after_sigint_aborts(restore_signal_handlers: None):
    """stop_process escalates SIGINT → SIGTERM; the SIGTERM is signal #2."""
    stop_event, abort_event = _installed()
    sigint = signal.getsignal(signal.SIGINT)
    sigterm = signal.getsignal(signal.SIGTERM)
    assert callable(sigint) and callable(sigterm)

    sigint(signal.SIGINT, None)
    sigterm(signal.SIGTERM, None)
    assert abort_event.is_set()


# ---------------------------------------------------------------------------
# End-to-end: real signals mid-session against a real UDS server
# ---------------------------------------------------------------------------


class _PacedSpeechStream:
    """Endless loud audio, ~3 ms per 30 ms read: capture keeps running until a
    signal stops it, and the interpreter hits bytecode boundaries constantly so
    a pending signal handler runs promptly."""

    def read(self, n_samples: int, exception_on_overflow: bool = False) -> bytes:
        time.sleep(0.003)
        return np.full(n_samples, 5000, dtype=np.int16).tobytes()

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakePyAudio:
    def open(self, **_kwargs: object) -> _PacedSpeechStream:
        return _PacedSpeechStream()

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
def hung_server(tmp_path: Path) -> Iterator[str]:
    """Consumes the request, then stalls — stands in for a hung server.

    The stall is an Event wait so teardown can release the handler thread
    before closing the server (closing under a live handler makes waitress
    traceback on its own trigger fd)."""
    release = threading.Event()
    app = Flask(__name__)

    @app.route("/transcribe_stream", methods=["POST"])
    def transcribe() -> object:
        request.stream.read()
        release.wait(timeout=5.0)
        return jsonify({"text": "too late"})

    def unstall() -> None:
        release.set()
        time.sleep(0.1)

    yield from _serve(tmp_path, app, on_teardown=unstall)


def _run_session(socket_path: str, tmp_path: Path):
    from talkat.client import TranscriptionClient
    from talkat.main import _set_stop_event_on_signal
    from talkat.session import DictationSession

    stop_event, abort_event = threading.Event(), threading.Event()
    _set_stop_event_on_signal(stop_event, abort_event)
    config = {"server_socket": socket_path, "http_timeout": 10}
    with TranscriptionClient(config) as client:
        session = DictationSession(
            client,
            threshold=200.0,
            max_duration=30.0,
            stop_event=stop_event,
            abort_event=abort_event,
            untranscribed_dir=tmp_path / "untranscribed",
        )
        return session.run(lambda _result: None), stop_event


def test_first_sigint_ends_capture_and_the_recording_is_transcribed(
    restore_signal_handlers: None, patched_audio: None, instant_server: str, tmp_path: Path
):
    """THE toggle regression test: SIGINT mid-capture must not lose the transcript."""
    timer = threading.Timer(0.35, os.kill, args=(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        outcome, stop_event = _run_session(instant_server, tmp_path)
    finally:
        timer.cancel()

    assert stop_event.is_set(), "signal was never delivered — test is broken"
    assert outcome.stop_reason == "stop_requested"
    assert outcome.text == "toggled text"
    assert not outcome.aborted


def test_second_signal_abandons_a_hung_server_and_saves_the_audio(
    restore_signal_handlers: None, patched_audio: None, hung_server: str, tmp_path: Path
):
    """stop_process sends SIGKILL a second after its SIGTERM: the abort must
    return promptly, with the audio it couldn't transcribe saved."""
    first = threading.Timer(0.35, os.kill, args=(os.getpid(), signal.SIGINT))
    second = threading.Timer(1.0, os.kill, args=(os.getpid(), signal.SIGTERM))
    first.start()
    second.start()
    started = time.monotonic()
    try:
        outcome, _ = _run_session(hung_server, tmp_path)
    finally:
        first.cancel()
        second.cancel()

    elapsed = time.monotonic() - started
    assert elapsed < 2.2, f"abort took {elapsed:.1f}s — past stop_process's SIGKILL window"
    assert outcome.aborted
    assert [r.failed for r in outcome.results] == [True]
    assert len(list((tmp_path / "untranscribed").glob("*.wav"))) == 1
