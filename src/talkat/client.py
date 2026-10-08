"""HTTP client for the model server's ``/transcribe_stream`` endpoint (unix socket)."""

import json
from types import TracebackType
from typing import Any

import httpx

from .config import CODE_DEFAULTS
from .segmenter import SAMPLE_RATE


class TranscriptionUnreachable(RuntimeError):
    """Server unreachable — connection refused, DNS failure, or request timeout."""


class TranscriptionServerError(RuntimeError):
    """Server returned an error (non-2xx, malformed JSON, etc.)."""


class TranscriptionClient:
    """POSTs 16 kHz mono int16 audio to the model server and returns the text."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.socket_path: str = config.get("server_socket", CODE_DEFAULTS["server_socket"])
        self.http_timeout: int = int(config.get("http_timeout", CODE_DEFAULTS["http_timeout"]))
        # Per-request language override sent in the stream metadata. The
        # server has its own config default; we only send this if the client
        # has one configured, so an older server build still works.
        self.language: str | None = config.get("language")
        # Server response metadata from the most recent call — audio duration,
        # applied gain, ASR wall-clock. Old servers without these fields leave
        # the values at 0.0.
        self.last_metadata: dict[str, float] = {
            "audio_duration": 0.0,
            "applied_gain_db": 0.0,
            "asr_seconds": 0.0,
        }
        transport = httpx.HTTPTransport(uds=self.socket_path)
        self._client = httpx.Client(transport=transport, timeout=self.http_timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TranscriptionClient":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def transcribe_audio(self, pcm: bytes, prompt: str | None = None) -> str:
        """Transcribe one buffer of 16 kHz mono int16 audio.

        ``prompt`` is the transcript just before this audio; the server feeds
        it to the model so a segment continues the previous one's sentence,
        casing, and vocabulary. Servers that predate the field ignore it.

        Raises ``TranscriptionUnreachable`` if the server can't be reached or
        times out, ``TranscriptionServerError`` for other server-side problems.
        """
        metadata: dict[str, Any] = {"rate": SAMPLE_RATE}
        if self.language:
            metadata["language"] = self.language
        if prompt:
            metadata["prompt"] = prompt
        body = json.dumps(metadata).encode("utf-8") + b"\n" + pcm

        # The host part of the URL is ignored when using a unix-socket transport;
        # only the path matters. We use a placeholder host purely for httpx hygiene.
        try:
            response = self._client.post("http://talkat/transcribe_stream", content=body)
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise TranscriptionUnreachable(
                f"Could not connect to the model server at {self.socket_path}. "
                "Ensure it's running: systemctl --user status talkat"
            ) from e
        except httpx.TimeoutException as e:
            raise TranscriptionUnreachable(f"Request to model server timed out: {e}") from e
        except httpx.HTTPError as e:
            raise TranscriptionServerError(f"Error communicating with model server: {e}") from e

        try:
            payload = response.json()
        except json.JSONDecodeError as e:
            raise TranscriptionServerError(
                f"Could not decode JSON response from server: {response.text}"
            ) from e

        self.last_metadata = {
            "audio_duration": float(payload.get("audio_duration", 0.0) or 0.0),
            "applied_gain_db": float(payload.get("applied_gain_db", 0.0) or 0.0),
            "asr_seconds": float(payload.get("asr_seconds", 0.0) or 0.0),
        }
        return str(payload.get("text", "")).strip()
