import json
import os
from typing import Any

from .logging_config import get_logger

logger = get_logger(__name__)

# Configuration for threshold storage
CONFIG_DIR = os.path.expanduser("~/.config/talkat")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# 1. CODE DEFAULTS
CODE_DEFAULTS: dict[str, Any] = {
    "silence_threshold": 200.0,
    "model_type": "faster-whisper",  # Options: faster-whisper, distil-whisper, vosk
    "model_name": "base.en",
    "faster_whisper_model_cache_dir": os.path.expanduser("~/.cache/talkat/faster-whisper"),
    "fw_device": "cpu",
    "fw_compute_type": "int8",
    "fw_device_index": 0,
    "vosk_model_base_dir": os.path.expanduser("~/.cache/talkat/vosk"),
    "clipboard_on_long": True,
    "save_transcripts": True,
    "transcript_dir": os.path.expanduser("~/.local/share/talkat/transcripts"),
    # New model-related options
    "distil_model_name": "distil-whisper/distil-medium.en",  # Medium model better for CPU
    "model_cache_dir": os.path.expanduser("~/.cache/talkat/models"),
    "device": "cpu",  # cpu, cuda, auto - defaulting to CPU for compatibility
}


def load_app_config() -> dict[str, Any]:
    """Loads the application configuration from a JSON file.
    Merges with code defaults, file values taking precedence.
    """
    config = CODE_DEFAULTS.copy()
    if os.path.exists(CONFIG_FILE):
        logger.debug(f"Loading config from {CONFIG_FILE}...")
        try:
            with open(CONFIG_FILE) as f:
                file_config = json.load(f)
            config.update(file_config)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.error(f"Error loading config from {CONFIG_FILE}: {e}. Using defaults.")
    else:
        logger.debug(f"No config file found at {CONFIG_FILE}. Using defaults.")
    return config


def save_app_config(config_dict: dict[str, Any]):
    """Saves the application configuration to a JSON file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config_dict, f, indent=4)
        logger.info(f"Configuration saved to {CONFIG_FILE}")
    except OSError as e:
        logger.error(f"Error saving config to {CONFIG_FILE}: {e}")
