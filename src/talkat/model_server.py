import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

import numpy as np
import vosk
from faster_whisper import WhisperModel
from flask import Flask, jsonify, request
from waitress import serve

from talkat.config import CODE_DEFAULTS, load_app_config
from talkat.logging_config import get_logger

try:
    import librosa
    import soundfile  # noqa: F401

    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False

logger = get_logger(__name__)


class ModelService:
    def __init__(self) -> None:
        self.model: Any = None
        self.model_type: str | None = None
        self.vosk_recognizer: Any = None
        self.dictionary_words: list[str] = []

    def initialize(self) -> None:
        logger.info("Initializing model for the server...")

        config = load_app_config()
        self.model_type = config.get("model_type", CODE_DEFAULTS["model_type"])
        model_name = config.get("model_name", CODE_DEFAULTS["model_name"])

        model_cache_dir = config.get("faster_whisper_model_cache_dir")
        fw_device = config.get("fw_device", CODE_DEFAULTS["fw_device"])
        fw_compute_type = config.get("fw_compute_type", CODE_DEFAULTS["fw_compute_type"])
        fw_device_index = config.get("fw_device_index", CODE_DEFAULTS["fw_device_index"])

        vosk_model_base_dir = config.get(
            "vosk_model_base_dir", CODE_DEFAULTS["vosk_model_base_dir"]
        )

        if self.model_type == "vosk":
            if vosk is None:
                logger.error("Vosk library is not installed. Cannot load Vosk model.")
                sys.exit(1)

            vosk_model_full_path = os.path.join(os.path.expanduser(vosk_model_base_dir), model_name)
            if not os.path.exists(vosk_model_full_path):
                logger.error(
                    f"Vosk model not found at {vosk_model_full_path}. Server cannot start."
                )
                sys.exit(1)

            logger.info(f"Loading Vosk model: {vosk_model_full_path}...")
            self.model = vosk.Model(vosk_model_full_path)
            # Sentinel recognizer used as an "init happened" guard; actual transcription
            # uses per-request recognizers for thread safety.
            self.vosk_recognizer = vosk.KaldiRecognizer(self.model, 16000)
            logger.info("Vosk model loaded.")

        elif self.model_type == "faster-whisper":
            if WhisperModel is None or np is None:
                logger.error(
                    "faster-whisper or numpy is not installed. Cannot load faster-whisper model."
                )
                sys.exit(1)

            logger.info(f"Loading faster-whisper model: {model_name}...")
            model_kwargs: dict[str, Any] = {
                "device": fw_device,
                "compute_type": fw_compute_type,
                "device_index": fw_device_index,
            }
            if model_cache_dir:
                logger.info(f"Using model cache directory: {model_cache_dir}")
                model_kwargs["download_root"] = model_cache_dir
            else:
                logger.info("Using default model cache directory for faster-whisper.")

            try:
                self.model = WhisperModel(model_name, **model_kwargs)
                logger.info(f"Faster-whisper model '{model_name}' loaded.")
            except Exception as e:
                logger.error(f"Error loading faster-whisper model '{model_name}': {e}")
                sys.exit(1)
        else:
            logger.error(
                f"Unsupported model type in config: {self.model_type}. Server cannot start."
            )
            sys.exit(1)

        self.load_dictionary()

        if self.model_type == "vosk" and self.vosk_recognizer:
            self.apply_vosk_dictionary(self.vosk_recognizer)

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
        """Build initial_prompt for faster-whisper from dictionary words."""
        if not self.dictionary_words:
            return None
        return ", ".join(self.dictionary_words)

    def apply_vosk_dictionary(self, recognizer: Any) -> None:
        """
        Apply dictionary to Vosk recognizer.

        Vosk's KaldiRecognizer uses the vocabulary from the loaded model and
        doesn't have direct support for vocabulary hints like Whisper's
        initial_prompt. SetGrammar() requires a JSON grammar format. For now
        this just logs that the dictionary is loaded but not applied.
        """
        if not self.dictionary_words:
            return

        logger.debug(
            f"Dictionary loaded with {len(self.dictionary_words)} words. "
            "Note: Vosk has limited dictionary support. "
            "Consider using faster-whisper for better custom vocabulary recognition."
        )


app = Flask(__name__)
_service = ModelService()


@app.route("/transcribe_stream", methods=["POST"])
def transcribe_audio_stream():
    if not _service.model:
        return jsonify({"error": "Model not loaded"}), 500

    try:
        metadata_line = request.stream.readline()
        if not metadata_line:
            return jsonify({"error": "Missing metadata line in stream"}), 400

        try:
            metadata_str = metadata_line.decode("utf-8").strip()
            metadata = json.loads(metadata_str)
            rate = int(metadata["rate"])
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            return jsonify({"error": f"Invalid or missing metadata: {e}"}), 400

        text_result = ""

        if _service.model_type == "vosk":
            if vosk is None:
                return jsonify({"error": "Vosk library not available"}), 500

            local_vosk_recognizer = vosk.KaldiRecognizer(_service.model, rate)

            while True:
                chunk = request.stream.read(4096)
                if not chunk:
                    break
                if local_vosk_recognizer.AcceptWaveform(chunk):
                    pass

            final_json = local_vosk_recognizer.Result()
            result_dict = json.loads(final_json)
            text_result = result_dict.get("text", "").strip()

        elif _service.model_type == "faster-whisper":
            if not isinstance(_service.model, WhisperModel):
                return jsonify({"error": "Faster-whisper model not correctly loaded"}), 500

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

            audio_bytes = bytes(audio_buffer)

            if not audio_bytes:
                text_result = ""
            else:
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                audio_np = np.ascontiguousarray(audio_np, dtype=np.float32)

                initial_prompt = _service.get_initial_prompt()
                segments, info = _service.model.transcribe(
                    audio_np,
                    language="en",
                    beam_size=5,
                    best_of=5,
                    vad_filter=True,
                    initial_prompt=initial_prompt,
                )
                logger.debug(
                    f"Streamed: Detected language '{info.language}' "
                    f"with probability {info.language_probability:.2f}"
                )
                recognized_texts = [segment.text for segment in segments]
                text_result = "".join(recognized_texts).strip()
        else:
            return jsonify(
                {"error": f"Unsupported model type configured on server: {_service.model_type}"}
            ), 500

        return jsonify({"text": text_result})

    except Exception as e:
        logger.error(f"Error during streaming transcription: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/transcribe_file", methods=["POST"])
def transcribe_file():
    """
    Transcribe an uploaded audio file.

    Accepts multipart/form-data with an 'audio' file field.
    Supports various audio formats (wav, mp3, flac, ogg, m4a, etc.)

    Returns JSON: {"text": transcription}
    """
    if not _service.model:
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

        text_result = ""

        if _service.model_type == "vosk":
            logger.info("Transcribing with Vosk model...")

            audio_int16 = (audio_data * 32768.0).astype(np.int16)
            audio_bytes = audio_int16.tobytes()

            local_vosk_recognizer = vosk.KaldiRecognizer(_service.model, sample_rate)

            if local_vosk_recognizer.AcceptWaveform(audio_bytes):
                final_json = local_vosk_recognizer.Result()
            else:
                final_json = local_vosk_recognizer.FinalResult()

            result_dict = json.loads(final_json)
            text_result = result_dict.get("text", "").strip()

            logger.info("Vosk transcription complete")

        elif _service.model_type == "faster-whisper":
            if not isinstance(_service.model, WhisperModel):
                return jsonify({"error": "Faster-whisper model not correctly loaded"}), 500

            logger.info("Transcribing with faster-whisper model...")

            if len(audio_data) == 0:
                text_result = ""
            else:
                initial_prompt = _service.get_initial_prompt()
                segments, info = _service.model.transcribe(
                    audio_data,
                    beam_size=5,
                    language="en",
                    vad_filter=True,
                    initial_prompt=initial_prompt,
                )
                logger.debug(
                    f"Detected language '{info.language}' "
                    f"with probability {info.language_probability:.2f}"
                )
                recognized_texts = [segment.text for segment in segments]
                text_result = "".join(recognized_texts).strip()

            logger.info("Faster-whisper transcription complete")

        else:
            return jsonify(
                {"error": f"Unsupported model type configured on server: {_service.model_type}"}
            ), 500

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
def health_check():
    if _service.model:
        return jsonify(
            {"status": "ok", "model_type": _service.model_type, "message": "Model loaded"}
        ), 200
    else:
        return jsonify({"status": "error", "message": "Model not loaded"}), 500


@app.route("/dictionary", methods=["GET"])
def get_dictionary():
    """Get current custom dictionary."""
    return jsonify(
        {"words": _service.dictionary_words, "count": len(_service.dictionary_words)}
    ), 200


@app.route("/dictionary", methods=["POST"])
def update_dictionary():
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

        if _service.model_type == "vosk" and _service.vosk_recognizer:
            _service.apply_vosk_dictionary(_service.vosk_recognizer)

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


def main():
    _service.initialize()
    config = load_app_config()
    socket_path = Path(config.get("server_socket", CODE_DEFAULTS["server_socket"]))

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
