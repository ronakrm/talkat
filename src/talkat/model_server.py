import base64
import json
import os
import sys
from typing import Any, Dict, Optional

import numpy as np
from flask import Flask, request, jsonify

from faster_whisper import WhisperModel
import vosk

from talkat.config import load_app_config, CODE_DEFAULTS

app = Flask(__name__)

# Global variable to hold the loaded model and its type
MODEL: Any = None
MODEL_TYPE: Optional[str] = None
MODEL_REC: Any = None # For Vosk recognizer

def initialize_model():
    global MODEL, MODEL_TYPE, MODEL_REC
    print("Initializing model for the server...")

    config = load_app_config()
    MODEL_TYPE = config.get('model_type', CODE_DEFAULTS['model_type'])
    model_name = config.get('model_name', CODE_DEFAULTS['model_name'])
    
    # Faster-Whisper specific args from config
    model_cache_dir = config.get('faster_whisper_model_cache_dir')
    fw_device = config.get('fw_device', CODE_DEFAULTS['fw_device'])
    fw_compute_type = config.get('fw_compute_type', CODE_DEFAULTS['fw_compute_type'])
    fw_device_index = config.get('fw_device_index', CODE_DEFAULTS['fw_device_index'])
    
    # Vosk specific args from config
    vosk_model_base_dir = config.get('vosk_model_base_dir', CODE_DEFAULTS['vosk_model_base_dir'])

    if MODEL_TYPE == "vosk":
        if vosk is None:
            print("Vosk library is not installed. Cannot load Vosk model.", file=sys.stderr)
            sys.exit(1)
        
        vosk_model_full_path = os.path.join(os.path.expanduser(vosk_model_base_dir), model_name)
        if not os.path.exists(vosk_model_full_path):
            print(f"Vosk model not found at {vosk_model_full_path}. Server cannot start.", file=sys.stderr)
            # Potentially download it here or provide better instructions.
            sys.exit(1)
        
        print(f"Loading Vosk model: {vosk_model_full_path}...")
        MODEL = vosk.Model(vosk_model_full_path)
        # MODEL_REC will be created per request or kept if suitable for concurrent use (Vosk docs needed)
        # For now, let's assume KaldiRecognizer might not be thread-safe or is better created per request.
        # If it can be reused, it should be initialized here.
        # Let's initialize it here assuming it can be reset or used for multiple inferences.
        MODEL_REC = vosk.KaldiRecognizer(MODEL, 16000) # Assuming 16kHz sample rate
        print("Vosk model loaded.")

    elif MODEL_TYPE == "faster-whisper":
        if WhisperModel is None or np is None:
            print("faster-whisper or numpy is not installed. Cannot load faster-whisper model.", file=sys.stderr)
            sys.exit(1)
        
        print(f"Loading faster-whisper model: {model_name}...")
        model_kwargs: Dict[str, Any] = {
            "device": fw_device,
            "compute_type": fw_compute_type,
            "device_index": fw_device_index
        }
        if model_cache_dir:
            print(f"Using model cache directory: {model_cache_dir}")
            model_kwargs["download_root"] = model_cache_dir
        else:
            print(f"Using default model cache directory for faster-whisper.")
        
        try:
            MODEL = WhisperModel(model_name, **model_kwargs)
            print(f"Faster-whisper model '{model_name}' loaded.")
        except Exception as e:
            print(f"Error loading faster-whisper model '{model_name}': {e}", file=sys.stderr)
            # Provide more specific advice based on common errors if possible
            sys.exit(1)
    else:
        print(f"Unsupported model type in config: {MODEL_TYPE}. Server cannot start.", file=sys.stderr)
        sys.exit(1)

@app.route('/transcribe', methods=['POST'])
def transcribe_audio():
    global MODEL, MODEL_TYPE, MODEL_REC
    if not MODEL:
        return jsonify({"error": "Model not loaded"}), 500

    data = request.get_json()
    if not data or 'audio_data_b64' not in data or 'rate' not in data:
        return jsonify({"error": "Missing audio_data_b64 or rate in request"}), 400

    try:
        audio_bytes_b64 = data['audio_data_b64']
        audio_bytes = base64.b64decode(audio_bytes_b64)
        rate = int(data['rate']) # Client should send the correct rate
        
        # For debugging:
        # print(f"Received {len(audio_bytes)} bytes of audio data, rate {rate} Hz.")

        text_result = ""

        if MODEL_TYPE == "vosk":
            if not MODEL_REC: # Should have been initialized by initialize_model
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
            chunk_size = 4000 # This matches main.py, but might not be needed if sending whole audio
            processed_once = False
            # The client sends the *entire* audio buffer.
            # We can feed it directly or in chunks if AcceptWaveform has limits.
            # Assuming we feed it all at once.
            if MODEL_REC.AcceptWaveform(audio_bytes):
                final_json = MODEL_REC.Result() # Get partial result
            else:
                final_json = MODEL_REC.FinalResult() # Get final result after all data
            
            result_dict = json.loads(final_json)
            text_result = result_dict.get('text', '').strip()

        elif MODEL_TYPE == "faster-whisper":
            if not isinstance(MODEL, WhisperModel): # Type check for safety
                return jsonify({"error": "Faster-whisper model not correctly loaded"}), 500

            # Convert audio_bytes (bytes) to NumPy array of floats
            if not audio_bytes:
                text_result = ""
            else:
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                segments, info = MODEL.transcribe(audio_np, beam_size=5) # beam_size from main.py
                # print(f"Detected language '{info.language}' with probability {info.language_probability:.2f}")
                recognized_texts = [segment.text for segment in segments]
                text_result = "".join(recognized_texts).strip()
        else:
            return jsonify({"error": f"Unsupported model type configured on server: {MODEL_TYPE}"}), 500

        return jsonify({"text": text_result})

    except Exception as e:
        print(f"Error during transcription: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/transcribe_stream', methods=['POST'])
def transcribe_audio_stream():
    global MODEL, MODEL_TYPE # MODEL_REC is not used here as we create a local one for Vosk streams
    if not MODEL:
        return jsonify({"error": "Model not loaded"}), 500

    try:
        # Read the first line for metadata (JSON)
        # request.stream is suitable for this.
        metadata_line = request.stream.readline()
        if not metadata_line:
            return jsonify({"error": "Missing metadata line in stream"}), 400
        
        try:
            metadata_str = metadata_line.decode('utf-8').strip()
            metadata = json.loads(metadata_str)
            rate = int(metadata['rate'])
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            return jsonify({"error": f"Invalid or missing metadata: {e}"}), 400

        text_result = ""

        if MODEL_TYPE == "vosk":
            if vosk is None: # Should have been checked at init, but good practice
                return jsonify({"error": "Vosk library not available"}), 500
            
            # Create a new recognizer for this stream for proper isolation
            local_vosk_recognizer = vosk.KaldiRecognizer(MODEL, rate)
            
            # Process audio chunks from the rest of the stream
            while True:
                chunk = request.stream.read(4096) # Read in chunks
                if not chunk:
                    break # End of stream
                # local_vosk_recognizer.AcceptWaveform(chunk)
                if local_vosk_recognizer.AcceptWaveform(chunk):
                    # Optionally, could use PartialResult for intermediate feedback
                    # For now, we wait for FinalResult
                    pass

            final_json = local_vosk_recognizer.Result()
            result_dict = json.loads(final_json)
            text_result = result_dict.get('text', '').strip()

        elif MODEL_TYPE == "faster-whisper":
            if not isinstance(MODEL, WhisperModel): # Type check
                return jsonify({"error": "Faster-whisper model not correctly loaded"}), 500

            audio_buffer = bytearray()
            while True:
                chunk = request.stream.read(4096)
                if not chunk:
                    break
                audio_buffer.extend(chunk)
            
            audio_bytes = bytes(audio_buffer)

            if not audio_bytes:
                text_result = ""
            else:
                # Convert accumulated audio_bytes to NumPy array of floats
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                audio_np = np.ascontiguousarray(audio_np, dtype=np.float32)
                # beam_size from main.py default, consider making it configurable for stream if needed
                segments, info = MODEL.transcribe(audio_np, language="en", beam_size=3, best_of=3) 
                # print(f"Streamed: Detected language '{info.language}' with probability {info.language_probability:.2f}")
                recognized_texts = [segment.text for segment in segments]
                text_result = "".join(recognized_texts).strip()
        else:
            return jsonify({"error": f"Unsupported model type configured on server: {MODEL_TYPE}"}), 500

        return jsonify({"text": text_result})

    except Exception as e:
        print(f"Error during streaming transcription: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    # Basic health check
    if MODEL:
        return jsonify({"status": "ok", "model_type": MODEL_TYPE, "message": "Model loaded"}), 200
    else:
        return jsonify({"status": "error", "message": "Model not loaded"}), 500

if __name__ == '__main__':
    initialize_model() # Load the model when the server starts
    if not MODEL:
        print("Model could not be initialized. Exiting server.", file=sys.stderr)
        sys.exit(1)
        
    print(f"Starting Flask server for Talkat model on port 5555...")
    app.run(host='127.0.0.1', port=5555, debug=False) # debug=False for production/background 