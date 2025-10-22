import base64
import json
import os
import sys
from typing import Any

import numpy as np
import vosk
from faster_whisper import WhisperModel
from flask import Flask, jsonify, request

from talkat.config import CODE_DEFAULTS, load_app_config
from talkat.logging_config import get_logger

# Optional librosa import for file processing
try:
    import librosa
    import soundfile  # noqa: F401

    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False

logger = get_logger(__name__)

app = Flask(__name__)

# Global variable to hold the loaded model and its type
MODEL: Any = None
MODEL_TYPE: str | None = None
MODEL_REC: Any = None  # For Vosk recognizer

# Global variable for custom dictionary
DICTIONARY_WORDS: list[str] = []


def load_dictionary() -> list[str]:
    """
    Load custom dictionary from file.

    Returns:
        List of dictionary words/phrases (one per line from file).
        Returns empty list if file doesn't exist or is empty.
    """
    global DICTIONARY_WORDS

    config = load_app_config()
    dictionary_file = config.get("dictionary_file")

    if not dictionary_file:
        logger.debug("No dictionary file configured")
        return []

    dictionary_path = os.path.expanduser(dictionary_file)

    if not os.path.exists(dictionary_path):
        logger.debug(f"Dictionary file not found: {dictionary_path}")
        return []

    try:
        with open(dictionary_path, encoding="utf-8") as f:
            # Read lines, strip whitespace, and filter out empty lines
            words = [line.strip() for line in f if line.strip()]

        logger.info(f"Loaded {len(words)} words/phrases from dictionary: {dictionary_path}")
        DICTIONARY_WORDS = words
        return words
    except Exception as e:
        logger.error(f"Error loading dictionary from {dictionary_path}: {e}")
        return []


def get_initial_prompt() -> str | None:
    """
    Build initial_prompt for faster-whisper from dictionary words.

    Returns:
        Comma-separated string of dictionary words for initial_prompt,
        or None if dictionary is empty.
    """
    global DICTIONARY_WORDS

    if not DICTIONARY_WORDS:
        return None

    # Join dictionary words with commas to create a hint for the model
    # Whisper uses this as context to improve recognition of these terms
    return ", ".join(DICTIONARY_WORDS)


def apply_vosk_dictionary(recognizer: Any) -> None:
    """
    Apply dictionary to Vosk recognizer.

    Note: Vosk has limited support for custom dictionaries. The KaldiRecognizer
    uses the vocabulary from the loaded model. To improve recognition of specific
    words, you would need to use Vosk grammars (SetGrammar) which requires JSON
    grammar specification, or retrain the model.

    For now, this function serves as a placeholder for future grammar support.
    Dictionary words are logged but not actively used by Vosk.

    Args:
        recognizer: Vosk KaldiRecognizer instance
    """
    global DICTIONARY_WORDS

    if not DICTIONARY_WORDS:
        return

    # Note: Vosk doesn't have direct dictionary support like Whisper's initial_prompt
    # SetWords() is for word-level timestamps, not vocabulary hints
    # SetGrammar() could be used but requires JSON grammar format
    # For now, we just log that dictionary is loaded but not applied to Vosk
    logger.debug(
        f"Dictionary loaded with {len(DICTIONARY_WORDS)} words. "
        "Note: Vosk has limited dictionary support. Consider using faster-whisper for better custom vocabulary recognition."
    )


def initialize_model():
    global MODEL, MODEL_TYPE, MODEL_REC
    logger.info("Initializing model for the server...")

    config = load_app_config()
    MODEL_TYPE = config.get("model_type", CODE_DEFAULTS["model_type"])
    model_name = config.get("model_name", CODE_DEFAULTS["model_name"])

    # Faster-Whisper specific args from config
    model_cache_dir = config.get("faster_whisper_model_cache_dir")
    fw_device = config.get("fw_device", CODE_DEFAULTS["fw_device"])
    fw_compute_type = config.get("fw_compute_type", CODE_DEFAULTS["fw_compute_type"])
    fw_device_index = config.get("fw_device_index", CODE_DEFAULTS["fw_device_index"])

    # Vosk specific args from config
    vosk_model_base_dir = config.get("vosk_model_base_dir", CODE_DEFAULTS["vosk_model_base_dir"])

    if MODEL_TYPE == "vosk":
        if vosk is None:
            logger.error("Vosk library is not installed. Cannot load Vosk model.")
            sys.exit(1)

        vosk_model_full_path = os.path.join(os.path.expanduser(vosk_model_base_dir), model_name)
        if not os.path.exists(vosk_model_full_path):
            logger.error(f"Vosk model not found at {vosk_model_full_path}. Server cannot start.")
            # Potentially download it here or provide better instructions.
            sys.exit(1)

        logger.info(f"Loading Vosk model: {vosk_model_full_path}...")
        MODEL = vosk.Model(vosk_model_full_path)
        # MODEL_REC will be created per request or kept if suitable for concurrent use (Vosk docs needed)
        # For now, let's assume KaldiRecognizer might not be thread-safe or is better created per request.
        # If it can be reused, it should be initialized here.
        # Let's initialize it here assuming it can be reset or used for multiple inferences.
        MODEL_REC = vosk.KaldiRecognizer(MODEL, 16000)  # Assuming 16kHz sample rate
        logger.info("Vosk model loaded.")

        # Note: Dictionary will be loaded after model initialization
        # apply_vosk_dictionary will be called if dictionary is available

    elif MODEL_TYPE == "faster-whisper":
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
            MODEL = WhisperModel(model_name, **model_kwargs)
            logger.info(f"Faster-whisper model '{model_name}' loaded.")
        except Exception as e:
            logger.error(f"Error loading faster-whisper model '{model_name}': {e}")
            # Provide more specific advice based on common errors if possible
            sys.exit(1)
    else:
        logger.error(f"Unsupported model type in config: {MODEL_TYPE}. Server cannot start.")
        sys.exit(1)

    # Load custom dictionary
    load_dictionary()

    # Apply dictionary to Vosk if using Vosk model
    if MODEL_TYPE == "vosk" and MODEL_REC:
        apply_vosk_dictionary(MODEL_REC)


@app.route("/transcribe", methods=["POST"])
def transcribe_audio():
    global MODEL, MODEL_TYPE, MODEL_REC
    if not MODEL:
        return jsonify({"error": "Model not loaded"}), 500

    data = request.get_json()
    if not data or "audio_data_b64" not in data or "rate" not in data:
        return jsonify({"error": "Missing audio_data_b64 or rate in request"}), 400

    try:
        audio_bytes_b64 = data["audio_data_b64"]
        audio_bytes = base64.b64decode(audio_bytes_b64)
        int(data["rate"])  # Client should send the correct rate

        # For debugging:
        logger.debug(f"Received {len(audio_bytes)} bytes of audio data, rate {data['rate']} Hz.")

        text_result = ""

        if MODEL_TYPE == "vosk":
            if not MODEL_REC:  # Should have been initialized by initialize_model
                return jsonify({"error": "Vosk recognizer not initialized"}), 500

            # Vosk's KaldiRecognizer needs to be reset or used carefully for multiple files.
            # If AcceptWaveform is called multiple times, it appends.
            # For a new transcription, ensure state is clean or create a new recognizer.
            # Let's re-create it for simplicity and safety, or ensure FinalResult resets it.
            # According to Vosk docs, FinalResult does reset the recognizer for the next utterance.
            # However, if the server serves multiple clients, thread safety of KaldiRecognizer instance is a concern.
            # For now, assuming single client or careful management.
            # A new recognizer for each request is safest if unsure.
            # local_rec = vosk.KaldiRecognizer(MODEL, rate) # Create fresh recognizer

            # Let's try using the global MODEL_REC and see. It might need locking for concurrent requests.
            # For now, process in chunks.
            # The client sends the *entire* audio buffer.
            # We can feed it directly or in chunks if AcceptWaveform has limits.
            # Assuming we feed it all at once.
            if MODEL_REC.AcceptWaveform(audio_bytes):
                final_json = MODEL_REC.Result()  # Get partial result
            else:
                final_json = MODEL_REC.FinalResult()  # Get final result after all data

            result_dict = json.loads(final_json)
            text_result = result_dict.get("text", "").strip()

        elif MODEL_TYPE == "faster-whisper":
            if not isinstance(MODEL, WhisperModel):  # Type check for safety
                return jsonify({"error": "Faster-whisper model not correctly loaded"}), 500

            # Convert audio_bytes (bytes) to NumPy array of floats
            if not audio_bytes:
                text_result = ""
            else:
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0

                # Use dictionary as initial_prompt if available
                initial_prompt = get_initial_prompt()
                segments, info = MODEL.transcribe(
                    audio_np, beam_size=5, initial_prompt=initial_prompt
                )
                logger.debug(
                    f"Detected language '{info.language}' with probability {info.language_probability:.2f}"
                )
                recognized_texts = [segment.text for segment in segments]
                text_result = "".join(recognized_texts).strip()
        else:
            return jsonify(
                {"error": f"Unsupported model type configured on server: {MODEL_TYPE}"}
            ), 500

        return jsonify({"text": text_result})

    except Exception as e:
        logger.error(f"Error during transcription: {e}")
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/transcribe_stream", methods=["POST"])
def transcribe_audio_stream():
    global MODEL, MODEL_TYPE  # MODEL_REC is not used here as we create a local one for Vosk streams
    if not MODEL:
        return jsonify({"error": "Model not loaded"}), 500

    try:
        # Read the first line for metadata (JSON)
        # request.stream is suitable for this.
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

        if MODEL_TYPE == "vosk":
            if vosk is None:  # Should have been checked at init, but good practice
                return jsonify({"error": "Vosk library not available"}), 500

            # Create a new recognizer for this stream for proper isolation
            local_vosk_recognizer = vosk.KaldiRecognizer(MODEL, rate)

            # Process audio chunks from the rest of the stream
            while True:
                chunk = request.stream.read(4096)  # Read in chunks
                if not chunk:
                    break  # End of stream
                # local_vosk_recognizer.AcceptWaveform(chunk)
                if local_vosk_recognizer.AcceptWaveform(chunk):
                    # Optionally, could use PartialResult for intermediate feedback
                    # For now, we wait for FinalResult
                    pass

            final_json = local_vosk_recognizer.Result()
            result_dict = json.loads(final_json)
            text_result = result_dict.get("text", "").strip()

        elif MODEL_TYPE == "faster-whisper":
            if not isinstance(MODEL, WhisperModel):  # Type check
                return jsonify({"error": "Faster-whisper model not correctly loaded"}), 500

            audio_buffer = bytearray()
            try:
                while True:
                    chunk = request.stream.read(4096)
                    if not chunk:
                        break
                    audio_buffer.extend(chunk)
            except (OSError, ValueError) as e:
                # Client disconnected abruptly
                logger.warning(f"Client disconnected during streaming: {e}")
                return jsonify({"error": "Client disconnected"}), 499

            audio_bytes = bytes(audio_buffer)

            if not audio_bytes:
                text_result = ""
            else:
                # Convert accumulated audio_bytes to NumPy array of floats
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                audio_np = np.ascontiguousarray(audio_np, dtype=np.float32)

                # Use dictionary as initial_prompt if available
                initial_prompt = get_initial_prompt()
                segments, info = MODEL.transcribe(
                    audio_np, language="en", beam_size=3, best_of=3, initial_prompt=initial_prompt
                )
                logger.debug(
                    f"Streamed: Detected language '{info.language}' with probability {info.language_probability:.2f}"
                )
                recognized_texts = [segment.text for segment in segments]
                text_result = "".join(recognized_texts).strip()
        else:
            return jsonify(
                {"error": f"Unsupported model type configured on server: {MODEL_TYPE}"}
            ), 500

        return jsonify({"text": text_result})

    except Exception as e:
        logger.error(f"Error during streaming transcription: {e}")
        import traceback

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
    global MODEL, MODEL_TYPE, MODEL_REC
    if not MODEL:
        return jsonify({"error": "Model not loaded"}), 500

    # Check if librosa is available
    if not LIBROSA_AVAILABLE:
        return jsonify(
            {
                "error": "librosa and soundfile are required for file processing",
                "hint": "Install them with: pip install librosa soundfile",
            }
        ), 500

    # Check if file was uploaded
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided in request"}), 400

    audio_file = request.files["audio"]

    if audio_file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    try:
        logger.info(f"Received file for transcription: {audio_file.filename}")

        # Save uploaded file to a temporary location (librosa can't read FileStorage directly)
        import tempfile

        # Get file extension, defaulting to empty string if filename is None
        file_ext = os.path.splitext(audio_file.filename or "")[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
            tmp_path = tmp_file.name
            audio_file.save(tmp_path)

        try:
            # Load audio file using librosa (auto-resamples to 16kHz mono)
            logger.info("Loading and resampling audio file to 16kHz mono...")
            audio_data, sample_rate = librosa.load(tmp_path, sr=16000, mono=True)
            duration = len(audio_data) / sample_rate
            logger.info(f"Audio loaded: {duration:.1f} seconds at {sample_rate} Hz")
        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        text_result = ""

        if MODEL_TYPE == "vosk":
            if not MODEL_REC:
                return jsonify({"error": "Vosk recognizer not initialized"}), 500

            logger.info("Transcribing with Vosk model...")

            # Convert float32 audio to int16 PCM format for Vosk
            audio_int16 = (audio_data * 32768.0).astype(np.int16)
            audio_bytes = audio_int16.tobytes()

            # Create a new recognizer for this file to avoid state issues
            local_vosk_recognizer = vosk.KaldiRecognizer(MODEL, sample_rate)

            # Process the entire audio file
            if local_vosk_recognizer.AcceptWaveform(audio_bytes):
                final_json = local_vosk_recognizer.Result()
            else:
                final_json = local_vosk_recognizer.FinalResult()

            result_dict = json.loads(final_json)
            text_result = result_dict.get("text", "").strip()

            logger.info("Vosk transcription complete")

        elif MODEL_TYPE == "faster-whisper":
            if not isinstance(MODEL, WhisperModel):
                return jsonify({"error": "Faster-whisper model not correctly loaded"}), 500

            logger.info("Transcribing with faster-whisper model...")

            # Audio is already in float32 format from librosa
            if len(audio_data) == 0:
                text_result = ""
            else:
                # Use dictionary as initial_prompt if available
                initial_prompt = get_initial_prompt()
                segments, info = MODEL.transcribe(
                    audio_data, beam_size=5, initial_prompt=initial_prompt
                )
                logger.debug(
                    f"Detected language '{info.language}' with probability {info.language_probability:.2f}"
                )
                recognized_texts = [segment.text for segment in segments]
                text_result = "".join(recognized_texts).strip()

            logger.info("Faster-whisper transcription complete")

        else:
            return jsonify(
                {"error": f"Unsupported model type configured on server: {MODEL_TYPE}"}
            ), 500

        logger.info(f"Transcription result: {len(text_result)} characters")
        return jsonify({"text": text_result})

    except Exception as e:
        logger.error(f"Error during file transcription: {e}")
        import traceback

        traceback.print_exc()

        # Provide more helpful error messages for common issues
        error_msg = str(e)
        if "NoBackendError" in str(type(e).__name__):
            error_msg = "No audio backend available. Install ffmpeg: sudo apt-get install ffmpeg"
        elif "UnsupportedFormat" in error_msg or "format" in error_msg.lower():
            error_msg = f"Unsupported audio format. Error: {error_msg}"

        return jsonify({"error": error_msg}), 500


@app.route("/health", methods=["GET"])
def health_check():
    # Basic health check
    if MODEL:
        return jsonify({"status": "ok", "model_type": MODEL_TYPE, "message": "Model loaded"}), 200
    else:
        return jsonify({"status": "error", "message": "Model not loaded"}), 500


@app.route("/dictionary", methods=["GET"])
def get_dictionary():
    """
    Get current custom dictionary.

    Returns:
        JSON: {"words": [list of dictionary words]}
    """
    global DICTIONARY_WORDS

    return jsonify({"words": DICTIONARY_WORDS, "count": len(DICTIONARY_WORDS)}), 200


@app.route("/dictionary", methods=["POST"])
def update_dictionary():
    """
    Update custom dictionary from uploaded text file.

    Accepts multipart/form-data with a 'dictionary' file field.
    File format: one word/phrase per line (plain text).

    Returns:
        JSON: {"success": True, "count": number of words loaded}
    """
    global DICTIONARY_WORDS, MODEL_REC, MODEL_TYPE

    # Check if file was uploaded
    if "dictionary" not in request.files:
        return jsonify({"error": "No dictionary file provided in request"}), 400

    dictionary_file = request.files["dictionary"]

    if dictionary_file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    try:
        # Read the uploaded file
        content = dictionary_file.read().decode("utf-8")
        lines = content.split("\n")

        # Filter out empty lines and strip whitespace
        words = [line.strip() for line in lines if line.strip()]

        if not words:
            logger.warning("Uploaded dictionary file is empty")
            return jsonify({"error": "Dictionary file is empty"}), 400

        # Get config to find dictionary file path
        config = load_app_config()
        dictionary_file_path = config.get("dictionary_file")
        if not dictionary_file_path:
            return jsonify({"error": "Dictionary file path not configured"}), 500
        dictionary_path = os.path.expanduser(str(dictionary_file_path))

        # Warn about overwrite if file exists
        if os.path.exists(dictionary_path):
            logger.warning(f"Overwriting existing dictionary file: {dictionary_path}")

        # Ensure directory exists
        os.makedirs(os.path.dirname(dictionary_path), exist_ok=True)

        # Save to file
        with open(dictionary_path, "w", encoding="utf-8") as f:
            f.write("\n".join(words))

        # Update global dictionary
        DICTIONARY_WORDS = words
        logger.info(f"Dictionary updated with {len(words)} words and saved to {dictionary_path}")

        # Re-apply dictionary to Vosk if using Vosk
        if MODEL_TYPE == "vosk" and MODEL_REC:
            apply_vosk_dictionary(MODEL_REC)

        return jsonify(
            {
                "success": True,
                "count": len(words),
                "message": f"Dictionary updated with {len(words)} words",
                "note": "faster-whisper will use these words as hints. Vosk has limited dictionary support.",
            }
        ), 200

    except UnicodeDecodeError:
        return jsonify({"error": "Dictionary file must be UTF-8 encoded text"}), 400
    except Exception as e:
        logger.error(f"Error updating dictionary: {e}")
        import traceback

        traceback.print_exc()
        return jsonify({"error": f"Failed to update dictionary: {str(e)}"}), 500


def main():
    initialize_model()
    config = load_app_config()
    host = config.get("server_host", CODE_DEFAULTS["server_host"])
    port = config.get("server_port", CODE_DEFAULTS["server_port"])
    logger.info(f"Starting Talkat model server on {host}:{port}")
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
