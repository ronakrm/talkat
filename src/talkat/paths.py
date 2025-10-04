"""Standard paths for Talkat following FHS and XDG specifications."""

import os
from pathlib import Path


def get_xdg_dir(env_var: str, default: str) -> Path:
    """Get XDG directory from environment or use default."""
    return Path(os.environ.get(env_var, os.path.expanduser(default)))


# XDG Base Directory Specification
XDG_CONFIG_HOME = get_xdg_dir("XDG_CONFIG_HOME", "~/.config")
XDG_CACHE_HOME = get_xdg_dir("XDG_CACHE_HOME", "~/.cache")
XDG_DATA_HOME = get_xdg_dir("XDG_DATA_HOME", "~/.local/share")
XDG_RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))

# Ensure runtime dir exists and fallback if not
if not XDG_RUNTIME_DIR.exists():
    XDG_RUNTIME_DIR = XDG_CACHE_HOME / "talkat" / "runtime"
    XDG_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

# Application-specific directories
APP_NAME = "talkat"

# Configuration files
CONFIG_DIR = XDG_CONFIG_HOME / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
DICTIONARY_FILE = CONFIG_DIR / "dictionary.txt"

# Cache directories
CACHE_DIR = XDG_CACHE_HOME / APP_NAME
MODEL_CACHE_DIR = CACHE_DIR / "models"
FASTER_WHISPER_CACHE_DIR = MODEL_CACHE_DIR / "faster-whisper"
VOSK_CACHE_DIR = MODEL_CACHE_DIR / "vosk"

# Data directories
DATA_DIR = XDG_DATA_HOME / APP_NAME
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
LOG_DIR = DATA_DIR / "logs"

# Runtime directories
RUNTIME_DIR = XDG_RUNTIME_DIR / APP_NAME
PID_DIR = RUNTIME_DIR
LOCK_DIR = RUNTIME_DIR

# System-wide paths (when installed system-wide)
SYSTEM_CONFIG_DIR = Path("/etc") / APP_NAME
SYSTEM_DATA_DIR = Path("/usr/share") / APP_NAME
SYSTEM_LIB_DIR = Path("/usr/lib") / APP_NAME


def ensure_user_directories():
    """Create all necessary user directories."""
    dirs = [
        CONFIG_DIR,
        CACHE_DIR,
        MODEL_CACHE_DIR,
        FASTER_WHISPER_CACHE_DIR,
        VOSK_CACHE_DIR,
        DATA_DIR,
        TRANSCRIPT_DIR,
        LOG_DIR,
        RUNTIME_DIR,
        PID_DIR,
        LOCK_DIR,
    ]
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)


def get_config_file() -> Path:
    """Get the configuration file path, checking system-wide first."""
    # Check user config first
    if CONFIG_FILE.exists():
        return CONFIG_FILE

    # Check system config as fallback
    system_config = SYSTEM_CONFIG_DIR / "config.json"
    if system_config.exists():
        return system_config

    # Return user config path for creation
    return CONFIG_FILE
