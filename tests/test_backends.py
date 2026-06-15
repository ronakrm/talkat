"""Unit tests for talkat.backends — factory dispatch and Protocol conformance.

These tests don't load real ASR models — that needs Whisper weights and a
Vosk model file on disk, neither of which CI has. We test the factory
dispatch + the contracts that don't need a loaded model:

  - create_backend returns the right class for each model_type
  - Unknown model_type raises ValueError
  - Backends conform to TranscriptionBackend Protocol (duck-typed)
  - Calling transcribe before load raises RuntimeError (lifecycle guard)
  - BackendLoadError is a RuntimeError subclass
  - VoskBackend.load raises BackendLoadError when the model path is missing
"""

from __future__ import annotations

import numpy as np
import pytest

from talkat.backends import (
    BackendLoadError,
    FasterWhisperBackend,
    TranscriptionBackend,
    VoskBackend,
    create_backend,
)


def test_create_backend_returns_faster_whisper_for_faster_whisper_type():
    backend = create_backend("faster-whisper")
    assert isinstance(backend, FasterWhisperBackend)
    assert backend.name == "faster-whisper"


def test_create_backend_returns_vosk_for_vosk_type():
    backend = create_backend("vosk")
    assert isinstance(backend, VoskBackend)
    assert backend.name == "vosk"


def test_create_backend_rejects_unknown_model_type():
    with pytest.raises(ValueError, match="Unknown model_type"):
        create_backend("gpt-5-whisper")


def test_create_backend_error_message_lists_known_backends():
    """The error message should help the user discover valid values."""
    with pytest.raises(ValueError, match="faster-whisper.*vosk|vosk.*faster-whisper"):
        create_backend("totally-made-up")


def test_BackendLoadError_is_runtime_error_subclass():
    """Callers may do ``except RuntimeError`` and still catch backend load failures."""
    assert issubclass(BackendLoadError, RuntimeError)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------
#
# Protocol membership is structural at runtime via @runtime_checkable, BUT
# TranscriptionBackend isn't marked runtime_checkable (Protocols with
# non-method members can't be — ``name`` is a class attribute). So we check
# the surface by hand: each backend must expose the four attributes/methods
# the Protocol declares.


@pytest.mark.parametrize("backend_cls", [FasterWhisperBackend, VoskBackend])
def test_backend_exposes_protocol_surface(backend_cls: type):
    """Every concrete backend must expose name, load, transcribe, warm_up."""
    instance = backend_cls()
    assert isinstance(instance.name, str) and instance.name, "name must be a non-empty str"
    assert callable(instance.load)
    assert callable(instance.transcribe)
    assert callable(instance.warm_up)


# ---------------------------------------------------------------------------
# Lifecycle guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_cls", [FasterWhisperBackend, VoskBackend])
def test_transcribe_before_load_raises_runtime_error(backend_cls: type):
    """Calling transcribe on an unloaded backend must fail loudly, not silently.

    A silent return would mask a server misconfiguration; a RuntimeError
    propagates to the Flask handler and becomes a 500 with a usable trace.
    """
    backend = backend_cls()
    audio = np.zeros(8000, dtype=np.float32)
    with pytest.raises(RuntimeError, match="before load"):
        backend.transcribe(audio)


@pytest.mark.parametrize("backend_cls", [FasterWhisperBackend, VoskBackend])
def test_warm_up_before_load_is_a_no_op(backend_cls: type):
    """warm_up before load must NOT raise — server init order may run warm_up
    speculatively, and the contract is 'failures are non-fatal'.
    """
    backend = backend_cls()
    # Should not raise; should be a clean no-op.
    backend.warm_up()


# ---------------------------------------------------------------------------
# VoskBackend.load error path
# ---------------------------------------------------------------------------


def test_vosk_load_raises_BackendLoadError_when_model_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """A missing Vosk model directory must surface as BackendLoadError.

    The Flask server catches BackendLoadError and exits with a clear log
    line; without the typed exception we'd see a generic FileNotFoundError
    that wouldn't be obviously a model problem.
    """
    backend = VoskBackend()
    config = {
        "vosk_model_base_dir": str(tmp_path / "nonexistent"),
        "model_name": "definitely-not-there",
    }
    with pytest.raises(BackendLoadError, match="Vosk model not found"):
        backend.load(config)


# ---------------------------------------------------------------------------
# Protocol type-narrowing (mypy-style assertion at runtime)
# ---------------------------------------------------------------------------


def test_factory_return_type_satisfies_Protocol():
    """The factory's annotated return type is TranscriptionBackend.

    A simple runtime check: the returned object has every attribute the
    Protocol declares (this is a smoke test against accidentally returning
    a None / Any from the factory).
    """
    backend: TranscriptionBackend = create_backend("faster-whisper")
    for attr in ("name", "load", "transcribe", "warm_up"):
        assert hasattr(backend, attr), f"backend is missing Protocol attr: {attr}"
