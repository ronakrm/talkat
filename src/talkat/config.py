import os
import json
import sys
from typing import Any, Dict

# Configuration for threshold storage
CONFIG_DIR = os.path.expanduser("~/.config/talkat")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# 1. CODE DEFAULTS
CODE_DEFAULTS: Dict[str, Any] = {
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
    "distil_model_name": "distil-whisper/distil-large-v3",
    "model_cache_dir": os.path.expanduser("~/.cache/talkat/models"),
    "device": "auto",  # auto, cpu, cuda
}

def load_app_config() -> Dict[str, Any]:
    """Loads the application configuration from a JSON file.
    Merges with code defaults, file values taking precedence.
    """
    config = CODE_DEFAULTS.copy()
    if os.path.exists(CONFIG_FILE):
        print(f"Loading config from {CONFIG_FILE}...")
        try:
            with open(CONFIG_FILE, 'r') as f:
                file_config = json.load(f)
            config.update(file_config)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            print(f"Error loading config from {CONFIG_FILE}: {e}. Using defaults.", file=sys.stderr)
    else:
        print(f"No config file found at {CONFIG_FILE}. Using defaults.")
    return config

def save_app_config(config_dict: Dict[str, Any]):
    """Saves the application configuration to a JSON file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_dict, f, indent=4)
        print(f"Configuration saved to {CONFIG_FILE}")
    except IOError as e:
        print(f"Error saving config to {CONFIG_FILE}: {e}", file=sys.stderr)

