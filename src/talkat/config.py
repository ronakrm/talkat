import json
from typing import Any

from .logging_config import get_logger
from .paths import (
    CONFIG_DIR,
    CONFIG_FILE,
    DICTIONARY_FILE,
    FASTER_WHISPER_CACHE_DIR,
    TRANSCRIPT_DIR,
    VOSK_CACHE_DIR,
    ensure_user_directories,
    get_config_file,
)

logger = get_logger(__name__)

# Ensure directories exist
ensure_user_directories()

# 1. CODE DEFAULTS
CODE_DEFAULTS: dict[str, Any] = {
    # Audio and Recognition Settings
    "silence_threshold": 200.0,
    "silence_duration": 3.0,  # Seconds of silence before stopping recording
    "pre_speech_padding": 0.3,  # Seconds of audio to keep before speech starts
    "silence_threshold_fallback": 500.0,  # Fallback threshold when auto-detection fails
    "silence_threshold_min": 50.0,  # Minimum allowed silence threshold
    "silence_threshold_max": 5000.0,  # Maximum allowed silence threshold
    
    # Recording Timeouts and Durations
    "max_recording_duration": 30.0,  # Max duration for short recordings (seconds)
    "long_mode_max_duration": 600.0,  # Max duration for long mode (10 minutes)
    
    # Server Configuration
    "server_host": "127.0.0.1",  # Server bind address
    "server_port": 5555,  # Server port
    "server_url": "http://127.0.0.1:5555",  # Complete server URL for clients
    
    # Network Timeouts
    "http_timeout": 120,  # General HTTP request timeout (seconds)
    "health_check_timeout": 2,  # Health check timeout (seconds)
    "file_processing_timeout_base": 30,  # Base timeout for file processing (seconds)
    
    # Process Management Timeouts
    "process_stop_timeout": 5.0,  # Max time to wait for process to stop
    "lock_acquire_timeout": 1.0,  # Max time to wait for lock acquisition
    "lock_retry_interval": 0.01,  # Sleep interval between lock acquisition attempts
    "process_check_interval": 0.1,  # Sleep interval when checking process status
    "background_process_delay": 0.5,  # Delay when stopping background processes
    
    # Model Configuration
    "model_type": "faster-whisper",  # Options: faster-whisper, distil-whisper, vosk
    "model_name": "base.en",
    "faster_whisper_model_cache_dir": str(FASTER_WHISPER_CACHE_DIR),
    "fw_device": "cpu",
    "fw_compute_type": "int8",
    "fw_device_index": 0,
    "vosk_model_base_dir": str(VOSK_CACHE_DIR),
    "distil_model_name": "distil-whisper/distil-medium.en",  # Medium model better for CPU
    "model_cache_dir": str(FASTER_WHISPER_CACHE_DIR.parent),
    "device": "cpu",  # cpu, cuda, auto - defaulting to CPU for compatibility
    
    # Application Features
    "clipboard_on_long": True,
    "save_transcripts": True,
    "transcript_dir": str(TRANSCRIPT_DIR),

    # Dictionary Configuration
    "dictionary_file": str(DICTIONARY_FILE),
}


def load_app_config() -> dict[str, Any]:
    """Loads the application configuration from a JSON file.
    Merges with code defaults, file values taking precedence.
    """
    from .security import validate_json_config

    config = CODE_DEFAULTS.copy()
    config_file = get_config_file()

    if config_file.exists():
        logger.debug(f"Loading config from {config_file}...")
        try:
            with open(config_file) as f:
                file_config = json.load(f)
            # Validate the loaded config
            file_config = validate_json_config(file_config)
            config.update(file_config)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error(f"Error loading config from {config_file}: {e}. Using defaults.")
    else:
        logger.debug(f"No config file found at {config_file}. Using defaults.")
    return config


def save_app_config(config_dict: dict[str, Any]):
    """Saves the application configuration to a JSON file."""
    from .security import validate_json_config

    # Validate config before saving
    config_dict = validate_json_config(config_dict)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config_dict, f, indent=4)
        logger.info(f"Configuration saved to {CONFIG_FILE}")
    except OSError as e:
        logger.error(f"Error saving config to {CONFIG_FILE}: {e}")
