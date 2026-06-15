"""Transcription backends — pluggable ASR implementations behind a Protocol.

The Protocol gives the Flask routes in ``model_server.py`` a single
``backend.transcribe(audio)`` call instead of an if/elif chain across
``model_type``. Adding a new backend (Parakeet, Distil-Whisper, OpenAI
API, etc.) means implementing this Protocol and registering it in
:func:`create_backend` — no other file in the project should need
``model_type`` branching after that.

Audio is always passed as ``np.float32`` mono at 16 kHz; clients
upstream of the backend (live mic, librosa.load) convert before
calling. Backends that natively use int16 (Vosk) handle the conversion
internally.

The Protocol is duck-typed (``typing.Protocol``, not ``ABC``) — backend
implementations don't need to inherit from anything. That keeps the
implementations decoupled from this module and easier to test.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

import numpy as np


class BackendLoadError(RuntimeError):
    """Raised when a backend can't initialize (missing model file, etc.)."""


class TranscriptionBackend(Protocol):
    """Pluggable ASR backend interface.

    Lifecycle:
        1. ``backend = create_backend(model_type)``
        2. ``backend.load(config)`` — once at server init
        3. ``backend.warm_up()`` — optional, runs a dummy inference
        4. ``backend.transcribe(audio, ...)`` — many times, per request

    Attributes:
        name: Human-readable backend name, used in log lines.
    """

    name: str

    def load(self, config: dict[str, Any]) -> None:
        """Load the model from configuration; called once at server start.

        Raises:
            BackendLoadError: if the model file is missing or unreadable.
        """
        ...

    def transcribe(
        self,
        audio: np.ndarray,
        language: str = "en",
        initial_prompt: str | None = None,
    ) -> str:
        """Transcribe a mono float32 16kHz audio buffer.

        Args:
            audio: float32 numpy array, mono, 16 kHz sample rate.
            language: BCP-47-ish code (e.g. "en"). Backends that don't
                support per-call language selection (Vosk's language is
                baked into the loaded model) may ignore this.
            initial_prompt: Optional dictionary/vocabulary hint. Backends
                without a prompt concept (Vosk) may ignore this.

        Returns:
            Transcribed text (whitespace-stripped), or "" for silence.
        """
        ...

    def warm_up(self) -> None:
        """Run a tiny dummy inference so the first real request hits a hot model.

        Without warm-up the first transcribe pays one-time JIT / kernel /
        cache costs (typically multiple seconds). Failures are non-fatal —
        the server should keep running even if warm-up couldn't allocate
        a buffer or something niche fails.
        """
        ...


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------


class FasterWhisperBackend:
    """faster-whisper (CTranslate2-backed Whisper) implementation."""

    name = "faster-whisper"

    def __init__(self) -> None:
        self._model: Any = None
        self._model_name: str = ""

    def load(self, config: dict[str, Any]) -> None:
        from faster_whisper import WhisperModel

        from .config import CODE_DEFAULTS

        model_name = config.get("model_name", CODE_DEFAULTS["model_name"])
        cache_dir = config.get("faster_whisper_model_cache_dir")
        device = config.get("fw_device", CODE_DEFAULTS["fw_device"])
        compute_type = config.get("fw_compute_type", CODE_DEFAULTS["fw_compute_type"])
        device_index = config.get("fw_device_index", CODE_DEFAULTS["fw_device_index"])

        kwargs: dict[str, Any] = {
            "device": device,
            "compute_type": compute_type,
            "device_index": device_index,
        }
        if cache_dir:
            kwargs["download_root"] = cache_dir

        try:
            self._model = WhisperModel(model_name, **kwargs)
            self._model_name = model_name
        except Exception as e:
            raise BackendLoadError(f"Could not load faster-whisper '{model_name}': {e}") from e

    def transcribe(
        self,
        audio: np.ndarray,
        language: str = "en",
        initial_prompt: str | None = None,
    ) -> str:
        if self._model is None:
            raise RuntimeError("FasterWhisperBackend.transcribe called before load()")
        if len(audio) == 0:
            return ""

        segments, _info = self._model.transcribe(
            audio,
            language=language,
            beam_size=5,
            best_of=5,
            vad_filter=True,
            initial_prompt=initial_prompt,
        )
        return "".join(seg.text for seg in segments).strip()

    def warm_up(self) -> None:
        if self._model is None:
            return
        # 0.5s of silence at 16kHz — enough to force a full inference path.
        dummy = np.zeros(8000, dtype=np.float32)
        segments, _ = self._model.transcribe(dummy, language="en", beam_size=1, vad_filter=False)
        for _ in segments:
            pass


class VoskBackend:
    """Vosk (Kaldi-backed) implementation.

    Vosk's language is baked into the loaded model — the ``language``
    arg on ``transcribe`` is intentionally ignored; pick a different
    Vosk model to change language. Vosk also has no native prompt /
    vocabulary hint (KaldiRecognizer.SetGrammar takes a different
    format), so ``initial_prompt`` is ignored too.
    """

    name = "vosk"

    def __init__(self) -> None:
        self._model: Any = None

    def load(self, config: dict[str, Any]) -> None:
        import vosk

        from .config import CODE_DEFAULTS

        model_name = config.get("model_name", CODE_DEFAULTS["model_name"])
        base_dir = config.get("vosk_model_base_dir", CODE_DEFAULTS["vosk_model_base_dir"])
        path = os.path.join(os.path.expanduser(base_dir), model_name)
        if not os.path.exists(path):
            raise BackendLoadError(f"Vosk model not found at {path}")

        try:
            self._model = vosk.Model(path)
        except Exception as e:
            raise BackendLoadError(f"Could not load Vosk model at {path}: {e}") from e

    def transcribe(
        self,
        audio: np.ndarray,
        language: str = "en",  # noqa: ARG002 — see class docstring
        initial_prompt: str | None = None,  # noqa: ARG002 — see class docstring
    ) -> str:
        import vosk

        if self._model is None:
            raise RuntimeError("VoskBackend.transcribe called before load()")
        if len(audio) == 0:
            return ""

        recognizer = vosk.KaldiRecognizer(self._model, 16000)
        audio_int16 = (audio * 32768.0).astype(np.int16)
        recognizer.AcceptWaveform(audio_int16.tobytes())
        result = json.loads(recognizer.FinalResult())
        return str(result.get("text", "")).strip()

    def warm_up(self) -> None:
        import vosk

        if self._model is None:
            return
        dummy = np.zeros(8000, dtype=np.float32)
        audio_int16 = (dummy * 32768.0).astype(np.int16)
        recognizer = vosk.KaldiRecognizer(self._model, 16000)
        recognizer.AcceptWaveform(audio_int16.tobytes())
        recognizer.FinalResult()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_backend(model_type: str) -> TranscriptionBackend:
    """Return the backend implementation for the given model_type string.

    Add new backends here. The Protocol means duck-typed instances work
    without inheritance; this factory is the only place that needs to
    know about every backend.
    """
    if model_type == "faster-whisper":
        return FasterWhisperBackend()
    if model_type == "vosk":
        return VoskBackend()
    raise ValueError(f"Unknown model_type: {model_type!r}. " "Known: 'faster-whisper', 'vosk'.")
