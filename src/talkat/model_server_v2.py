"""Enhanced model server with support for multiple speech-to-text models."""

import base64
import json
import logging
import os
import sys
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, Optional, Tuple, Union

import numpy as np
from flask import Flask, request, jsonify, Response

from talkat.config import load_app_config, CODE_DEFAULTS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)


class ModelBackend(ABC):
    """Abstract base class for speech-to-text model backends."""
    
    @abstractmethod
    def transcribe(self, audio_data: np.ndarray, sample_rate: int) -> str:
        """Transcribe audio data to text."""
        pass
    
    @abstractmethod
    def transcribe_stream(self, audio_chunks: Generator[bytes, None, None], sample_rate: int) -> str:
        """Transcribe streaming audio chunks to text."""
        pass
    
    @abstractmethod
    def get_info(self) -> Dict[str, Any]:
        """Get information about the loaded model."""
        pass
    
    def cleanup(self) -> None:
        """Cleanup resources if needed."""
        pass


class FasterWhisperBackend(ModelBackend):
    """Faster-Whisper model backend."""
    
    def __init__(self, model_name: str, **kwargs):
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise ImportError("faster-whisper is not installed")
        
        logger.info(f"Loading Faster-Whisper model: {model_name}")
        self.model_name = model_name
        
        # Extract configuration
        self.device = kwargs.get('device', 'auto')
        self.compute_type = kwargs.get('compute_type', 'default')
        self.device_index = kwargs.get('device_index', 0)
        self.cache_dir = kwargs.get('cache_dir')
        
        model_kwargs = {
            "device": self.device,
            "compute_type": self.compute_type,
            "device_index": self.device_index
        }
        
        if self.cache_dir:
            model_kwargs["download_root"] = self.cache_dir
            logger.info(f"Using cache directory: {self.cache_dir}")
        
        try:
            self.model = WhisperModel(model_name, **model_kwargs)
            logger.info(f"Faster-Whisper model '{model_name}' loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load Faster-Whisper model: {e}")
            raise
    
    def transcribe(self, audio_data: np.ndarray, sample_rate: int) -> str:
        """Transcribe audio data using Faster-Whisper."""
        if len(audio_data) == 0:
            return ""
        
        # Ensure audio is normalized float32
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32) / 32768.0
        
        segments, info = self.model.transcribe(
            audio_data, 
            beam_size=5,
            language="en",
            condition_on_previous_text=False,
            log_prob_threshold=-1.0
        )
        
        logger.debug(f"Language: {info.language}, Probability: {info.language_probability:.2f}")
        
        recognized_texts = [segment.text for segment in segments]
        return "".join(recognized_texts).strip()
    
    def transcribe_stream(self, audio_chunks: Generator[bytes, None, None], sample_rate: int) -> str:
        """Transcribe streaming audio using Faster-Whisper."""
        audio_buffer = bytearray()
        
        for chunk in audio_chunks:
            audio_buffer.extend(chunk)
        
        if not audio_buffer:
            return ""
        
        audio_np = np.frombuffer(bytes(audio_buffer), dtype=np.int16).astype(np.float32) / 32768.0
        return self.transcribe(audio_np, sample_rate)
    
    def get_info(self) -> Dict[str, Any]:
        """Get model information."""
        return {
            "type": "faster-whisper",
            "model": self.model_name,
            "device": self.device,
            "compute_type": self.compute_type
        }


class DistilWhisperBackend(ModelBackend):
    """Distil-Whisper model backend for improved performance."""
    
    def __init__(self, model_name: str = "distil-whisper/distil-large-v3", **kwargs):
        try:
            import torch
            from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor
        except ImportError:
            raise ImportError("transformers and torch are not installed")
        
        logger.info(f"Loading Distil-Whisper model: {model_name}")
        self.model_name = model_name
        
        # Determine device
        self.device = kwargs.get('device', 'cuda' if torch.cuda.is_available() else 'cpu')
        self.torch_dtype = torch.float16 if self.device == 'cuda' else torch.float32
        
        cache_dir = kwargs.get('cache_dir')
        
        try:
            # Load model and processor
            self.model = AutoModelForSpeechSeq2Seq.from_pretrained(
                model_name,
                torch_dtype=self.torch_dtype,
                low_cpu_mem_usage=True,
                use_safetensors=True,
                cache_dir=cache_dir
            )
            self.model.to(self.device)
            
            self.processor = AutoProcessor.from_pretrained(
                model_name,
                cache_dir=cache_dir
            )
            
            logger.info(f"Distil-Whisper model loaded on {self.device}")
        except Exception as e:
            logger.error(f"Failed to load Distil-Whisper model: {e}")
            raise
    
    def transcribe(self, audio_data: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe audio data using Distil-Whisper."""
        import torch
        
        if len(audio_data) == 0:
            return ""
        
        # Ensure audio is normalized float32
        if audio_data.dtype != np.float32:
            audio_data = audio_data.astype(np.float32) / 32768.0
        
        # Process audio through the model
        inputs = self.processor(
            audio_data, 
            sampling_rate=sample_rate, 
            return_tensors="pt"
        ).to(self.device)
        
        with torch.no_grad():
            generated_ids = self.model.generate(
                **inputs,
                max_new_tokens=448,
                do_sample=False,
                language="en"
            )
        
        transcription = self.processor.batch_decode(
            generated_ids, 
            skip_special_tokens=True
        )[0]
        
        return transcription.strip()
    
    def transcribe_stream(self, audio_chunks: Generator[bytes, None, None], sample_rate: int) -> str:
        """Transcribe streaming audio using Distil-Whisper."""
        audio_buffer = bytearray()
        
        for chunk in audio_chunks:
            audio_buffer.extend(chunk)
        
        if not audio_buffer:
            return ""
        
        audio_np = np.frombuffer(bytes(audio_buffer), dtype=np.int16).astype(np.float32) / 32768.0
        return self.transcribe(audio_np, sample_rate)
    
    def get_info(self) -> Dict[str, Any]:
        """Get model information."""
        return {
            "type": "distil-whisper",
            "model": self.model_name,
            "device": str(self.device),
            "dtype": str(self.torch_dtype)
        }


class VoskBackend(ModelBackend):
    """Vosk model backend."""
    
    def __init__(self, model_name: str, **kwargs):
        try:
            import vosk
        except ImportError:
            raise ImportError("vosk is not installed")
        
        model_base_dir = kwargs.get('model_base_dir', CODE_DEFAULTS['vosk_model_base_dir'])
        model_path = Path(model_base_dir).expanduser() / model_name
        
        if not model_path.exists():
            raise FileNotFoundError(f"Vosk model not found at {model_path}")
        
        logger.info(f"Loading Vosk model: {model_path}")
        
        try:
            self.model = vosk.Model(str(model_path))
            self.model_path = model_path
            logger.info("Vosk model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load Vosk model: {e}")
            raise
    
    def transcribe(self, audio_data: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe audio data using Vosk."""
        import vosk
        
        if len(audio_data) == 0:
            return ""
        
        # Convert to bytes if needed
        if audio_data.dtype == np.float32:
            audio_bytes = (audio_data * 32768.0).astype(np.int16).tobytes()
        else:
            audio_bytes = audio_data.tobytes()
        
        recognizer = vosk.KaldiRecognizer(self.model, sample_rate)
        recognizer.AcceptWaveform(audio_bytes)
        result = json.loads(recognizer.FinalResult())
        
        return result.get('text', '').strip()
    
    def transcribe_stream(self, audio_chunks: Generator[bytes, None, None], sample_rate: int) -> str:
        """Transcribe streaming audio using Vosk."""
        import vosk
        
        recognizer = vosk.KaldiRecognizer(self.model, sample_rate)
        
        for chunk in audio_chunks:
            recognizer.AcceptWaveform(chunk)
        
        result = json.loads(recognizer.FinalResult())
        return result.get('text', '').strip()
    
    def get_info(self) -> Dict[str, Any]:
        """Get model information."""
        return {
            "type": "vosk",
            "model_path": str(self.model_path)
        }


class ModelServer:
    """Main model server managing different backends."""
    
    def __init__(self):
        self.backend: Optional[ModelBackend] = None
        self.config: Dict[str, Any] = {}
    
    def initialize(self) -> None:
        """Initialize the model server with configured backend."""
        self.config = load_app_config()
        model_type = self.config.get('model_type', CODE_DEFAULTS['model_type'])
        model_name = self.config.get('model_name', CODE_DEFAULTS['model_name'])
        
        logger.info(f"Initializing model server with {model_type} backend")
        
        try:
            if model_type == "faster-whisper":
                self.backend = FasterWhisperBackend(
                    model_name,
                    device=self.config.get('fw_device', CODE_DEFAULTS['fw_device']),
                    compute_type=self.config.get('fw_compute_type', CODE_DEFAULTS['fw_compute_type']),
                    device_index=self.config.get('fw_device_index', CODE_DEFAULTS['fw_device_index']),
                    cache_dir=self.config.get('faster_whisper_model_cache_dir')
                )
            elif model_type == "distil-whisper":
                # Use distil-whisper specific config or defaults
                distil_model = self.config.get('distil_model_name', 'distil-whisper/distil-large-v3')
                self.backend = DistilWhisperBackend(
                    distil_model,
                    device=self.config.get('device', 'auto'),
                    cache_dir=self.config.get('model_cache_dir')
                )
            elif model_type == "vosk":
                self.backend = VoskBackend(
                    model_name,
                    model_base_dir=self.config.get('vosk_model_base_dir', CODE_DEFAULTS['vosk_model_base_dir'])
                )
            else:
                raise ValueError(f"Unsupported model type: {model_type}")
            
            logger.info("Model server initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize model server: {e}")
            raise
    
    def transcribe(self, audio_data: Union[bytes, np.ndarray], sample_rate: int) -> str:
        """Transcribe audio data."""
        if self.backend is None:
            raise RuntimeError("Model backend not initialized")
        
        # Convert bytes to numpy array if needed
        if isinstance(audio_data, bytes):
            audio_data = np.frombuffer(audio_data, dtype=np.int16)
        
        return self.backend.transcribe(audio_data, sample_rate)
    
    def transcribe_stream(self, audio_chunks: Generator[bytes, None, None], sample_rate: int) -> str:
        """Transcribe streaming audio."""
        if self.backend is None:
            raise RuntimeError("Model backend not initialized")
        
        return self.backend.transcribe_stream(audio_chunks, sample_rate)
    
    def get_info(self) -> Dict[str, Any]:
        """Get server and model information."""
        if self.backend is None:
            return {"status": "not_initialized"}
        
        return {
            "status": "ready",
            "backend": self.backend.get_info(),
            "config": {
                "model_type": self.config.get('model_type'),
                "model_name": self.config.get('model_name')
            }
        }
    
    def cleanup(self) -> None:
        """Cleanup resources."""
        if self.backend:
            self.backend.cleanup()


# Global server instance
server = ModelServer()


@app.route('/transcribe', methods=['POST'])
def transcribe_audio() -> Tuple[Response, int]:
    """Transcribe audio from base64 encoded data."""
    if server.backend is None:
        return jsonify({"error": "Model not loaded"}), 500
    
    data = request.get_json()
    if not data or 'audio_data_b64' not in data or 'rate' not in data:
        return jsonify({"error": "Missing audio_data_b64 or rate in request"}), 400
    
    try:
        audio_bytes = base64.b64decode(data['audio_data_b64'])
        sample_rate = int(data['rate'])
        
        logger.debug(f"Received {len(audio_bytes)} bytes at {sample_rate} Hz")
        
        text = server.transcribe(audio_bytes, sample_rate)
        
        return jsonify({"text": text}), 200
    
    except Exception as e:
        logger.error(f"Error during transcription: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/transcribe_stream', methods=['POST'])
def transcribe_audio_stream() -> Tuple[Response, int]:
    """Transcribe streaming audio data."""
    if server.backend is None:
        return jsonify({"error": "Model not loaded"}), 500
    
    try:
        # Read metadata from first line
        metadata_line = request.stream.readline()
        if not metadata_line:
            return jsonify({"error": "Missing metadata line"}), 400
        
        metadata = json.loads(metadata_line.decode('utf-8').strip())
        sample_rate = int(metadata['rate'])
        
        logger.debug(f"Starting stream transcription at {sample_rate} Hz")
        
        # Generator for audio chunks
        def audio_chunk_generator():
            while True:
                chunk = request.stream.read(4096)
                if not chunk:
                    break
                yield chunk
        
        text = server.transcribe_stream(audio_chunk_generator(), sample_rate)
        
        return jsonify({"text": text}), 200
    
    except Exception as e:
        logger.error(f"Error during stream transcription: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/transcribe_file', methods=['POST'])
def transcribe_file() -> Tuple[Response, int]:
    """Transcribe audio from uploaded file."""
    if server.backend is None:
        return jsonify({"error": "Model not loaded"}), 500
    
    if 'audio' not in request.files:
        return jsonify({"error": "No audio file provided"}), 400
    
    audio_file = request.files['audio']
    if audio_file.filename == '':
        return jsonify({"error": "No file selected"}), 400
    
    try:
        import librosa
        import soundfile as sf
        
        # Save uploaded file temporarily
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=Path(audio_file.filename).suffix, delete=False) as tmp_file:
            audio_file.save(tmp_file.name)
            tmp_path = tmp_file.name
        
        try:
            # Load audio file with librosa
            audio_data, sample_rate = librosa.load(tmp_path, sr=16000, mono=True)
            
            logger.debug(f"Loaded audio file: {len(audio_data)} samples at {sample_rate} Hz")
            
            # Transcribe
            text = server.transcribe(audio_data, sample_rate)
            
            return jsonify({"text": text, "duration": len(audio_data) / sample_rate}), 200
        
        finally:
            # Clean up temporary file
            Path(tmp_path).unlink(missing_ok=True)
    
    except Exception as e:
        logger.error(f"Error transcribing file: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/health', methods=['GET'])
def health_check() -> Tuple[Response, int]:
    """Health check endpoint."""
    info = server.get_info()
    
    if info.get('status') == 'ready':
        return jsonify(info), 200
    else:
        return jsonify(info), 503


@app.route('/models', methods=['GET'])
def list_models() -> Tuple[Response, int]:
    """List available model types."""
    models = {
        "available": ["faster-whisper", "distil-whisper", "vosk"],
        "current": server.config.get('model_type') if server.backend else None,
        "recommended": "distil-whisper"
    }
    return jsonify(models), 200


def main():
    """Main entry point for the model server."""
    try:
        server.initialize()
        logger.info("Starting Flask server on http://127.0.0.1:5555")
        app.run(host='127.0.0.1', port=5555, threaded=True)
    except Exception as e:
        logger.error(f"Server failed to start: {e}")
        sys.exit(1)
    finally:
        server.cleanup()


if __name__ == "__main__":
    main()