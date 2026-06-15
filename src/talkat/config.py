import json
from typing import Any

from .logging_config import get_logger
from .paths import (
    CONFIG_DIR,
    CONFIG_FILE,
    DICTIONARY_FILE,
    FASTER_WHISPER_CACHE_DIR,
    SOCKET_FILE,
    TRANSCRIPT_DIR,
    VOSK_CACHE_DIR,
    get_config_files,
)

logger = get_logger(__name__)

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
    # Long mode auto-stops after this much continuous silence (no speech detected).
    "long_mode_silence_timeout": 60.0,
    # Hard cap on a single long-mode session.
    "long_mode_max_session_duration": 1800.0,  # 30 minutes
    # Trip the long-mode circuit breaker after this many consecutive server errors.
    "long_mode_max_consecutive_errors": 5,
    # Server Configuration
    "server_socket": str(SOCKET_FILE),  # Unix domain socket path for the model server
    # Network Timeouts (apply to local unix-socket requests)
    "http_timeout": 120,  # General request timeout (seconds)
    "health_check_timeout": 2,  # Health check timeout (seconds)
    "file_processing_timeout_base": 30,  # Base timeout for file processing (seconds)
    # Server limits
    "max_upload_size_mb": 100,  # Reject /transcribe_file uploads larger than this
    # Process Management Timeouts
    "process_stop_timeout": 5.0,  # Max time to wait for process to stop
    "lock_acquire_timeout": 1.0,  # Max time to wait for lock acquisition
    "lock_retry_interval": 0.01,  # Sleep interval between lock acquisition attempts
    "process_check_interval": 0.1,  # Sleep interval when checking process status
    "background_process_delay": 0.5,  # Delay when stopping background processes
    # Model Configuration
    "model_type": "faster-whisper",  # Options: faster-whisper, vosk
    "model_name": "small.en",
    "faster_whisper_model_cache_dir": str(FASTER_WHISPER_CACHE_DIR),
    "fw_device": "cpu",
    "fw_compute_type": "int8",
    "fw_device_index": 0,
    "vosk_model_base_dir": str(VOSK_CACHE_DIR),
    "model_cache_dir": str(FASTER_WHISPER_CACHE_DIR.parent),
    "device": "cpu",  # cpu, cuda, auto - defaulting to CPU for compatibility
    # Language passed to the ASR backend. "auto" → autodetect (faster-whisper);
    # Vosk ignores this (language is baked into the loaded model).
    "language": "en",
    # Application Features
    "clipboard_on_long": True,
    "save_transcripts": True,
    "transcript_dir": str(TRANSCRIPT_DIR),
    # Dictionary Configuration
    "dictionary_file": str(DICTIONARY_FILE),
    # AI Post-Processing (AIPP) — opt-in, off by default.
    # Map of profile-name → {base_url, model, system_prompt, api_key_env?, timeout?}.
    # Activated per-invocation with `--postprocess <name>`; see security.py
    # ``validate_postprocess_profile`` for the full schema.
    "postprocess_profiles": {},
}


def load_app_config() -> dict[str, Any]:
    """Load the effective configuration by merging all available layers.

    Layers, lowest to highest precedence:
        1. ``CODE_DEFAULTS`` (built into the package)
        2. ``/etc/talkat/config.json`` (system, optional — set by packagers)
        3. ``~/.config/talkat/config.json`` (per-user, optional)

    Each layer partially overrides the previous, so a user can override
    one key without restating the rest. A malformed layer logs an error
    and is skipped — the remaining layers still apply.

    CLI-level overrides (``--max-recording`` etc.) are merged on top of
    the result by callers in ``cli.py``; they do not live here.
    """
    from .security import validate_json_config

    config = CODE_DEFAULTS.copy()
    layers = get_config_files()
    if not layers:
        logger.debug("No config files found. Using code defaults.")
        return config

    for path in layers:
        logger.debug(f"Loading config from {path}...")
        try:
            with open(path) as f:
                layer = json.load(f)
            layer = validate_json_config(layer)
            config.update(layer)
        except (json.JSONDecodeError, ValueError, TypeError, OSError) as e:
            logger.error(f"Error loading config from {path}: {e}. Skipping this layer.")
    return config


def save_app_config(config_dict: dict[str, Any]) -> None:
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
