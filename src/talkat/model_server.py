import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np
from flask import Flask, jsonify, request
from flask.typing import ResponseReturnValue
from waitress import serve

from talkat.backends import BackendLoadError, TranscriptionBackend, create_backend
from talkat.config import CODE_DEFAULTS, load_app_config
from talkat.logging_config import get_logger
from talkat.security import validate_language

try:
    import librosa
    import soundfile  # noqa: F401

    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False

logger = get_logger(__name__)


class ModelService:
    """Server-side orchestration around a single ``TranscriptionBackend``.

    Holds the loaded backend, the dictionary state, and the model_type for
    introspection. All ASR work is delegated to ``self.backend.transcribe``;
    this class doesn't know about Whisper or Vosk specifics — backends do.
    """

    def __init__(self) -> None:
        self.backend: TranscriptionBackend | None = None
        self.model_type: str | None = None
        self.dictionary_words: list[str] = []
        # Server-side fallback when a request omits language. Per-request
        # values from /transcribe_stream and /transcribe_file override this.
        self.default_language: str = CODE_DEFAULTS["language"]

    def initialize(self) -> None:
        logger.info("Initializing model for the server...")

        config = load_app_config()
        # Local var first so mypy sees a concrete str at the create_backend
        # call; self.model_type stays str | None to preserve the "not loaded"
        # sentinel for callers that introspect post-construction.
        model_type: str = config.get("model_type", CODE_DEFAULTS["model_type"])
        self.model_type = model_type
        model_name = config.get("model_name", CODE_DEFAULTS["model_name"])
        self.default_language = str(config.get("language", CODE_DEFAULTS["language"]))

        try:
            self.backend = create_backend(model_type)
        except ValueError as e:
            logger.error(str(e))
            sys.exit(1)

        logger.info(f"Loading {self.backend.name} model: {model_name}")
        try:
            self.backend.load(config)
        except BackendLoadError as e:
            logger.error(str(e))
            sys.exit(1)
        logger.info(f"{self.backend.name} model loaded.")

        self.load_dictionary()
        self._warm_up()

    def _warm_up(self) -> None:
        """Run a tiny dummy inference so the first real request hits a hot model.

        Failures are non-fatal — the server should keep accepting requests.
        """
        if self.backend is None:
            return
        try:
            self.backend.warm_up()
            logger.info(f"{self.backend.name} model warmed up.")
        except Exception as e:
            logger.warning(f"Model warm-up failed: {e}; first request may be slow")

    def load_dictionary(self) -> list[str]:
        """Load custom dictionary from configured file path."""
        config = load_app_config()
        dictionary_file = config.get("dictionary_file")

        if not dictionary_file:
            logger.debug("No dictionary file configured")
            self.dictionary_words = []
            return []

        dictionary_path = os.path.expanduser(dictionary_file)

        if not os.path.exists(dictionary_path):
            logger.debug(f"Dictionary file not found: {dictionary_path}")
            self.dictionary_words = []
            return []

        try:
            with open(dictionary_path, encoding="utf-8") as f:
                words = [line.strip() for line in f if line.strip()]

            logger.info(f"Loaded {len(words)} words/phrases from dictionary: {dictionary_path}")
            self.dictionary_words = words
            return words
        except Exception as e:
            logger.error(f"Error loading dictionary from {dictionary_path}: {e}")
            self.dictionary_words = []
            return []

    def get_initial_prompt(self) -> str | None:
        """Build initial_prompt for faster-whisper from dictionary words.

        Vosk ignores ``initial_prompt`` (see VoskBackend.transcribe). Other
        future backends decide for themselves what to do with this hint.
        """
        if not self.dictionary_words:
            return None
        return ", ".join(self.dictionary_words)


app = Flask(__name__)
_service = ModelService()


def _resolve_language(raw: object) -> str:
    """Pick the language for a single request.

    Per-request value (from metadata / form field) wins over the server-side
    default. We re-validate even though the client already did — never trust
    the wire. Raises ValueError on malformed input.
    """
    if raw is None or raw == "":
        return _service.default_language
    if not isinstance(raw, str):
        raise ValueError(f"language must be a string, got {type(raw).__name__}")
    return validate_language(raw)


@app.errorhandler(413)
def _request_too_large(_e: Exception) -> ResponseReturnValue:
    limit_mb = app.config.get("MAX_CONTENT_LENGTH", 0) // (1024 * 1024)
    return jsonify({"error": f"Upload exceeds maximum allowed size of {limit_mb} MB"}), 413


@app.route("/transcribe_stream", methods=["POST"])
def transcribe_audio_stream() -> ResponseReturnValue:
    if _service.backend is None:
        return jsonify({"error": "Model not loaded"}), 500

    try:
        metadata_line = request.stream.readline()
        if not metadata_line:
            return jsonify({"error": "Missing metadata line in stream"}), 400

        try:
            metadata = json.loads(metadata_line.decode("utf-8").strip())
            # ``rate`` parsed but currently fixed at 16 kHz throughout the
            # client; we still validate the field exists so a malformed
            # client gets a clear 400 rather than a transcribe failure.
            int(metadata["rate"])
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            return jsonify({"error": f"Invalid or missing metadata: {e}"}), 400

        try:
            language = _resolve_language(metadata.get("language"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        audio_buffer = bytearray()
        try:
            while True:
                chunk = request.stream.read(4096)
                if not chunk:
                    break
                audio_buffer.extend(chunk)
        except (OSError, ValueError) as e:
            logger.warning(f"Client disconnected during streaming: {e}")
            return jsonify({"error": "Client disconnected"}), 499

        if not audio_buffer:
            return jsonify({"text": ""})

        audio_np = np.frombuffer(bytes(audio_buffer), dtype=np.int16).astype(np.float32) / 32768.0
        audio_np = np.ascontiguousarray(audio_np, dtype=np.float32)

        text_result = _service.backend.transcribe(
            audio_np,
            language=language,
            initial_prompt=_service.get_initial_prompt(),
        )
        return jsonify({"text": text_result})

    except Exception as e:
        logger.error(f"Error during streaming transcription: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/transcribe_file", methods=["POST"])
def transcribe_file() -> ResponseReturnValue:
    """Transcribe an uploaded audio file.

    Accepts multipart/form-data with an 'audio' file field. Various audio
    formats (wav, mp3, flac, ogg, m4a, …) — librosa handles decoding.

    Returns JSON: {"text": transcription}
    """
    if _service.backend is None:
        return jsonify({"error": "Model not loaded"}), 500

    if not LIBROSA_AVAILABLE:
        return jsonify(
            {
                "error": "librosa and soundfile are required for file processing",
                "hint": "Install them with: pip install librosa soundfile",
            }
        ), 500

    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided in request"}), 400

    audio_file = request.files["audio"]

    if audio_file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    try:
        language = _resolve_language(request.form.get("language"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        logger.info(f"Received file for transcription: {audio_file.filename}")

        # librosa can't read FileStorage directly, so persist to a temp file first.
        file_ext = os.path.splitext(audio_file.filename or "")[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
            tmp_path = tmp_file.name
            audio_file.save(tmp_path)

        try:
            logger.info("Loading and resampling audio file to 16kHz mono...")
            audio_data, sample_rate = librosa.load(tmp_path, sr=16000, mono=True)
            duration = len(audio_data) / sample_rate
            logger.info(f"Audio loaded: {duration:.1f} seconds at {sample_rate} Hz")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        text_result = _service.backend.transcribe(
            audio_data,
            language=language,
            initial_prompt=_service.get_initial_prompt(),
        )

        logger.info(f"Transcription result: {len(text_result)} characters")
        return jsonify({"text": text_result})

    except Exception as e:
        logger.error(f"Error during file transcription: {e}")
        traceback.print_exc()

        error_msg = str(e)
        if "NoBackendError" in str(type(e).__name__):
            error_msg = "No audio backend available. Install ffmpeg: sudo apt-get install ffmpeg"
        elif "UnsupportedFormat" in error_msg or "format" in error_msg.lower():
            error_msg = f"Unsupported audio format. Error: {error_msg}"

        return jsonify({"error": error_msg}), 500


@app.route("/health", methods=["GET"])
def health_check() -> ResponseReturnValue:
    if _service.backend is not None:
        return jsonify(
            {"status": "ok", "model_type": _service.model_type, "message": "Model loaded"}
        ), 200
    else:
        return jsonify({"status": "error", "message": "Model not loaded"}), 500


@app.route("/dictionary", methods=["GET"])
def get_dictionary() -> ResponseReturnValue:
    """Get current custom dictionary."""
    return jsonify(
        {"words": _service.dictionary_words, "count": len(_service.dictionary_words)}
    ), 200


@app.route("/dictionary", methods=["POST"])
def update_dictionary() -> ResponseReturnValue:
    """
    Update custom dictionary from uploaded text file.

    Accepts multipart/form-data with a 'dictionary' file field.
    File format: one word/phrase per line (plain text).

    Returns:
        JSON: {"success": True, "count": number of words loaded}
    """
    if "dictionary" not in request.files:
        return jsonify({"error": "No dictionary file provided in request"}), 400

    dictionary_file = request.files["dictionary"]

    if dictionary_file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    try:
        content = dictionary_file.read().decode("utf-8")
        lines = content.split("\n")

        words = [line.strip() for line in lines if line.strip()]

        if not words:
            logger.warning("Uploaded dictionary file is empty")
            return jsonify({"error": "Dictionary file is empty"}), 400

        config = load_app_config()
        dictionary_file_path = config.get("dictionary_file")
        if not dictionary_file_path:
            return jsonify({"error": "Dictionary file path not configured"}), 500
        dictionary_path = os.path.expanduser(str(dictionary_file_path))

        if os.path.exists(dictionary_path):
            logger.warning(f"Overwriting existing dictionary file: {dictionary_path}")

        os.makedirs(os.path.dirname(dictionary_path), exist_ok=True)

        with open(dictionary_path, "w", encoding="utf-8") as f:
            f.write("\n".join(words))

        _service.dictionary_words = words
        logger.info(f"Dictionary updated with {len(words)} words and saved to {dictionary_path}")

        return jsonify(
            {
                "success": True,
                "count": len(words),
                "message": f"Dictionary updated with {len(words)} words",
                "note": "faster-whisper will use these words as hints. "
                "Vosk has limited dictionary support.",
            }
        ), 200

    except UnicodeDecodeError:
        return jsonify({"error": "Dictionary file must be UTF-8 encoded text"}), 400
    except Exception as e:
        logger.error(f"Error updating dictionary: {e}")
        traceback.print_exc()
        return jsonify({"error": f"Failed to update dictionary: {str(e)}"}), 500


def main() -> None:
    _service.initialize()
    config = load_app_config()
    socket_path = Path(config.get("server_socket", CODE_DEFAULTS["server_socket"]))

    max_size_mb = int(config.get("max_upload_size_mb", CODE_DEFAULTS["max_upload_size_mb"]))
    app.config["MAX_CONTENT_LENGTH"] = max_size_mb * 1024 * 1024
    logger.info(f"Upload size limit: {max_size_mb} MB")

    # Ensure the runtime dir exists and remove any stale socket left over from
    # a previous crash. waitress recreates it on bind.
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()

    logger.info(f"Starting Talkat model server on {socket_path}")
    # waitress serializes requests on a single thread by default, which keeps
    # model/recognizer state safe without explicit locking. unix_socket_perms
    # 0600 restricts the socket to the owning user.
    serve(app, unix_socket=str(socket_path), unix_socket_perms="0600")


if __name__ == "__main__":
    main()
